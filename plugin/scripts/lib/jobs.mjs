// plugin/scripts/lib/jobs.mjs
//
// Per-workspace job registry for Grok companion runs (status / result / cancel).
// Mirrors the codex-plugin job idea without depending on Codex. Plugin-local
// state only; safety still lives in the wrapper (hardened mode).

import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { resolveWorkspaceRoot } from "./gate-state.mjs";
import {
  parseTargetFlag,
  resolveTargetWorkspaceRoot,
} from "./git-context.mjs";
import {
  isNotificationMode,
  NOTIFICATION_MODES,
  parseNotificationMode,
  parseWebhookUrl,
} from "./notification-modes.mjs";

export { isNotificationMode, NOTIFICATION_MODES, parseNotificationMode, parseWebhookUrl };

const PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA";
const FALLBACK = path.join(os.tmpdir(), "grok-companion");
const MAX_JOBS = 50;
const JOB_ID_RE = /^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$/;
const DIR_MODE = 0o700;
const FILE_MODE = 0o600;

/** Single source of jobs-index config defaults (design §11). */
export const DEFAULT_JOBS_CONFIG = Object.freeze({
  runMode: "hardened",
  notificationMode: "off",
  notificationWebhookUrl: null,
  lastRescueJobId: null,
  // Integration (how edits land) is orthogonal to runMode (security posture).
  integrationMode: "direct",
  integrationConsent: false,
});

/** Integration modes for code/implement (how edits land). Not runMode. */
export const INTEGRATION_MODES = Object.freeze([
  "direct",
  "worktree",
  "auto",
  "review",
]);

/**
 * @param {unknown} value
 * @returns {"direct"|"worktree"|"auto"|"review"|null}
 */
export function parseIntegrationMode(value) {
  const v = String(value ?? "")
    .trim()
    .toLowerCase();
  return INTEGRATION_MODES.includes(v) ? v : null;
}

/**
 * Claude Code exports userConfig values as CLAUDE_PLUGIN_OPTION_<KEY> with the
 * schema key uppercased (runMode -> RUNMODE). Also accept underscore forms
 * (RUN_MODE) when trivially cheap - host docs are ambiguous on camelCase keys.
 */
const PLUGIN_OPTION_RUNMODE_KEYS = ["RUNMODE", "RUN_MODE"];
const PLUGIN_OPTION_NOTIFICATIONMODE_KEYS = ["NOTIFICATIONMODE", "NOTIFICATION_MODE"];
const PLUGIN_OPTION_WEBHOOK_KEYS = [
  "NOTIFICATIONWEBHOOKURL",
  "NOTIFICATION_WEBHOOK_URL",
];
const PLUGIN_OPTION_INTEGRATIONMODE_KEYS = ["INTEGRATIONMODE", "INTEGRATION_MODE"];

/** Normalize stored/corrupt config values to a known mode (default off). */
function normalizeNotificationMode(value) {
  return parseNotificationMode(value) ?? DEFAULT_JOBS_CONFIG.notificationMode;
}

/** Stored corrupt webhook URLs fall back to null. */
function normalizeWebhookUrl(value) {
  const parsed = parseWebhookUrl(value);
  return parsed.ok ? parsed.url : null;
}

/**
 * @param {unknown} raw
 * @param {{ legacySetup?: boolean }} [opts]
 *   legacySetup: index file pre-dates prefsSources; treat stored prefs as setup.
 */
function normalizeConfig(raw, opts = {}) {
  let prefsSources = {};
  if (raw?.prefsSources && typeof raw.prefsSources === "object") {
    prefsSources = { ...raw.prefsSources };
  } else if (opts.legacySetup) {
    // Pre-userConfig indexes: saveIndex persists config on EVERY job, so a
    // workspace that merely ran a job (never setup) carries default values.
    // Only pin a field as setup-authored when its stored value is NON-default
    // (evidence of a deliberate setup); otherwise leave it unset so post-upgrade
    // CLAUDE_PLUGIN_OPTION_* userConfig still applies.
    const defaultNotificationMode = normalizeNotificationMode(undefined);
    if (raw?.runMode === "direct") prefsSources.runMode = "setup";
    if (normalizeNotificationMode(raw?.notificationMode) !== defaultNotificationMode) {
      prefsSources.notificationMode = "setup";
    }
    if (normalizeWebhookUrl(raw?.notificationWebhookUrl)) {
      prefsSources.notificationWebhookUrl = "setup";
    }
  }
  return {
    runMode: raw?.runMode === "direct" ? "direct" : "hardened",
    notificationMode: normalizeNotificationMode(raw?.notificationMode),
    notificationWebhookUrl: normalizeWebhookUrl(raw?.notificationWebhookUrl),
    lastRescueJobId: raw?.lastRescueJobId ?? null,
    integrationMode:
      parseIntegrationMode(raw?.integrationMode) ?? DEFAULT_JOBS_CONFIG.integrationMode,
    integrationConsent: raw?.integrationConsent === true,
    prefsSources,
  };
}

function isSetupAuthored(config, key) {
  return config?.prefsSources?.[key] === "setup";
}

/**
 * First non-empty CLAUDE_PLUGIN_OPTION_<suffix> among candidate suffixes.
 * @returns {{ name: string, value: string } | null}
 */
function readPluginOption(env, suffixes) {
  for (const suffix of suffixes) {
    const name = `CLAUDE_PLUGIN_OPTION_${suffix}`;
    const raw = env?.[name];
    if (raw == null) continue;
    const value = String(raw).trim();
    if (!value) continue;
    return { name, value };
  }
  return null;
}

function noteInvalidPluginOption(name, value) {
  try {
    process.stderr.write(
      `[grok-jobs] ignoring invalid ${name}=${JSON.stringify(value)}; using setup prefs or default\n`
    );
  } catch {
    /* best-effort */
  }
}

export function isValidJobId(jobId) {
  return typeof jobId === "string" && JOB_ID_RE.test(jobId);
}

function mkdirPrivate(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: DIR_MODE });
  try {
    fs.chmodSync(dir, DIR_MODE);
  } catch {
    /* best-effort on platforms without chmod */
  }
}

function writePrivate(filePath, content) {
  fs.writeFileSync(filePath, content, { encoding: "utf8", mode: FILE_MODE });
  try {
    fs.chmodSync(filePath, FILE_MODE);
  } catch {
    /* best-effort */
  }
}

function assertJobIdSafe(jobId) {
  if (!isValidJobId(jobId)) {
    throw new Error(`invalid job id: ${jobId}`);
  }
  return jobId;
}

function nowIso() {
  return new Date().toISOString();
}

/**
 * Per-workspace state segment: `<basename-slug>-<sha256(canonical)[0:16]>`.
 * Kept identical for legacy tmp and CLAUDE_PLUGIN_DATA layouts so migration
 * and dual-path lookups share one key.
 */
function workspaceStateSegment(cwd) {
  const workspaceRoot = resolveWorkspaceRoot(cwd);
  let canonical = workspaceRoot;
  try {
    canonical = fs.realpathSync.native(workspaceRoot);
  } catch {
    canonical = workspaceRoot;
  }
  const slug =
    (path.basename(workspaceRoot) || "workspace")
      .replace(/[^a-zA-Z0-9._-]+/g, "-")
      .replace(/^-+|-+$/g, "") || "workspace";
  const hash = createHash("sha256").update(canonical).digest("hex").slice(0, 16);
  return `${slug}-${hash}`;
}

/**
 * Absolute CLAUDE_PLUGIN_DATA (or PLUGIN_DATA) only. Relative / empty -> null.
 * Host fact: Claude exports ~/.claude/plugins/data/<id>/ as an absolute path.
 */
function resolvePluginDataDir(env = process.env) {
  const raw = (env[PLUGIN_DATA_ENV] ?? env.PLUGIN_DATA ?? "").trim();
  if (!raw || !path.isAbsolute(raw)) {
    return null;
  }
  return raw;
}

/**
 * Atomic file copy via temp + rename (same filesystem). Destination path is
 * the complete-marker for migration: only written after body copy attempts.
 */
function atomicCopyFile(src, dest) {
  const dir = path.dirname(dest);
  mkdirPrivate(dir);
  const tmp = path.join(
    dir,
    `.${path.basename(dest)}.tmp-${process.pid}-${randomBytes(4).toString("hex")}`
  );
  try {
    fs.copyFileSync(src, tmp);
    try {
      fs.chmodSync(tmp, FILE_MODE);
    } catch {
      /* best-effort */
    }
    fs.renameSync(tmp, dest);
  } catch (err) {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* best-effort cleanup */
    }
    throw err;
  }
}

/**
 * Best-effort recursive copy of a job body directory (copy, never move).
 * Per-entry failures are noted on stderr; caller decides completeness.
 */
function copyJobBodyTree(srcDir, destDir) {
  mkdirPrivate(destDir);
  const entries = fs.readdirSync(srcDir, { withFileTypes: true });
  for (const entry of entries) {
    const from = path.join(srcDir, entry.name);
    const to = path.join(destDir, entry.name);
    if (entry.isDirectory()) {
      copyJobBodyTree(from, to);
    } else if (entry.isFile()) {
      fs.copyFileSync(from, to);
      try {
        fs.chmodSync(to, FILE_MODE);
      } catch {
        /* best-effort */
      }
    }
  }
}

/**
 * Best-effort migration of jobs-index.json + jobs/<id>/ bodies from the legacy
 * tmp root into CLAUDE_PLUGIN_DATA/state. Complete only when the new
 * jobs-index.json exists (dir-exists alone is not enough - retry partials).
 * Index is written last via temp+rename so interrupted copies stay retryable.
 * Legacy is left in place as a frozen snapshot (copy, not move). Never throws.
 */
function maybeMigrateLegacyState(legacyDir, newDir) {
  try {
    const newIndex = path.join(newDir, "jobs-index.json");
    // Complete-marker: index presence. Dir-without-index is retryable.
    if (fs.existsSync(newIndex)) {
      return;
    }
    if (!fs.existsSync(legacyDir)) {
      return;
    }
    const legacyIndex = path.join(legacyDir, "jobs-index.json");
    if (!fs.existsSync(legacyIndex)) {
      return;
    }
    mkdirPrivate(newDir);

    // Job bodies first (best-effort per entry). Partial bodies still allow the
    // index write; individual entry failures are noted but do not abort.
    const legacyJobs = path.join(legacyDir, "jobs");
    const newJobs = path.join(newDir, "jobs");
    if (fs.existsSync(legacyJobs)) {
      mkdirPrivate(newJobs);
      let entries = [];
      try {
        entries = fs.readdirSync(legacyJobs, { withFileTypes: true });
      } catch (err) {
        process.stderr.write(
          `[grok-jobs] job body migration partial (list): ${err?.message ?? err}\n`
        );
        entries = [];
      }
      for (const entry of entries) {
        try {
          const from = path.join(legacyJobs, entry.name);
          const to = path.join(newJobs, entry.name);
          if (entry.isDirectory()) {
            copyJobBodyTree(from, to);
          } else if (entry.isFile()) {
            fs.copyFileSync(from, to);
            try {
              fs.chmodSync(to, FILE_MODE);
            } catch {
              /* best-effort */
            }
          }
        } catch (err) {
          try {
            process.stderr.write(
              `[grok-jobs] job body migration partial for ${entry.name}: ${err?.message ?? err}\n`
            );
          } catch {
            /* best-effort */
          }
        }
      }
    }

    // Index last = complete marker. Atomic rename keeps partials retryable.
    atomicCopyFile(legacyIndex, newIndex);
    process.stderr.write(
      `[grok-jobs] migrated workspace state from ${legacyDir} to ${newDir}\n`
    );
  } catch (err) {
    try {
      process.stderr.write(
        `[grok-jobs] state migration skipped: ${err?.message ?? err}\n`
      );
    } catch {
      /* best-effort */
    }
  }
}
function stateRoot(cwd, env = process.env) {
  const segment = workspaceStateSegment(cwd);
  const legacyDir = path.join(FALLBACK, segment);
  const pluginData = resolvePluginDataDir(env);
  if (pluginData) {
    const newDir = path.join(pluginData, "state", segment);
    maybeMigrateLegacyState(legacyDir, newDir);
    return newDir;
  }
  return legacyDir;
}

export function jobsDir(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), "jobs");
}

function indexPath(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), "jobs-index.json");
}

function ensure(cwd, env = process.env) {
  mkdirPrivate(stateRoot(cwd, env));
  mkdirPrivate(jobsDir(cwd, env));
}

function emptyConfig() {
  return normalizeConfig({ ...DEFAULT_JOBS_CONFIG, prefsSources: {} });
}

function loadIndex(cwd, env = process.env) {
  ensure(cwd, env);
  const file = indexPath(cwd, env);
  if (!fs.existsSync(file)) {
    return { version: 1, jobs: [], config: emptyConfig() };
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    const legacySetup =
      parsed?.config != null &&
      (parsed.config.prefsSources === undefined || parsed.config.prefsSources === null);
    return {
      version: 1,
      jobs: Array.isArray(parsed.jobs) ? parsed.jobs : [],
      config: normalizeConfig(parsed.config, { legacySetup }),
    };
  } catch {
    return { version: 1, jobs: [], config: emptyConfig() };
  }
}

function saveIndex(cwd, index, env = process.env) {
  ensure(cwd, env);
  const jobs = [...(index.jobs ?? [])]
    .sort((a, b) => String(b.updatedAt ?? "").localeCompare(String(a.updatedAt ?? "")))
    .slice(0, MAX_JOBS);
  const config = normalizeConfig(index.config);
  // Always persist prefsSources (possibly {}) so new indexes are not mistaken
  // for pre-userConfig legacy files on the next load.
  const payload = {
    version: 1,
    config: {
      runMode: config.runMode,
      notificationMode: config.notificationMode,
      notificationWebhookUrl: config.notificationWebhookUrl,
      lastRescueJobId: config.lastRescueJobId,
      integrationMode: config.integrationMode,
      integrationConsent: config.integrationConsent === true,
      prefsSources: config.prefsSources ?? {},
    },
    jobs,
  };
  writePrivate(indexPath(cwd, env), `${JSON.stringify(payload, null, 2)}\n`);
  return payload;
}

/**
 * Effective run mode.
 * Precedence: GROK_SKILLS_MODE (process override) > setup prefs >
 * CLAUDE_PLUGIN_OPTION_RUNMODE env > built-in default.
 */
export function getRunMode(cwd, env = process.env) {
  const fromEnv = (env.GROK_SKILLS_MODE ?? "").trim().toLowerCase();
  if (fromEnv === "direct" || fromEnv === "hardened") {
    return fromEnv;
  }
  const config = loadIndex(cwd, env).config;
  if (isSetupAuthored(config, "runMode")) {
    return config.runMode === "direct" ? "direct" : "hardened";
  }
  const opt = readPluginOption(env, PLUGIN_OPTION_RUNMODE_KEYS);
  if (opt) {
    const mode = opt.value.toLowerCase();
    if (mode === "direct" || mode === "hardened") {
      return mode;
    }
    noteInvalidPluginOption(opt.name, opt.value);
  }
  return DEFAULT_JOBS_CONFIG.runMode;
}

export function setRunMode(cwd, mode, env = process.env) {
  const index = loadIndex(cwd, env);
  index.config.runMode = mode === "direct" ? "direct" : "hardened";
  index.config.prefsSources = { ...(index.config.prefsSources ?? {}), runMode: "setup" };
  saveIndex(cwd, index, env);
  return index.config.runMode;
}

/**
 * Effective integration mode (how edits land: direct|worktree|auto|review).
 * Precedence: setup prefs > CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE > default.
 * Orthogonal to runMode. Env alone is a default hint, never consent.
 * @returns {"direct"|"worktree"|"auto"|"review"}
 */
export function getIntegrationMode(cwd, env = process.env) {
  const config = loadIndex(cwd, env).config;
  if (isSetupAuthored(config, "integrationMode")) {
    return (
      parseIntegrationMode(config.integrationMode) ?? DEFAULT_JOBS_CONFIG.integrationMode
    );
  }
  const opt = readPluginOption(env, PLUGIN_OPTION_INTEGRATIONMODE_KEYS);
  if (opt) {
    const mode = parseIntegrationMode(opt.value);
    if (mode) {
      return mode;
    }
    noteInvalidPluginOption(opt.name, opt.value);
  }
  return DEFAULT_JOBS_CONFIG.integrationMode;
}

/**
 * True only when setup --integration direct recorded operator consent.
 * Env / userConfig alone never satisfies this gate.
 */
export function getIntegrationConsent(cwd, env = process.env) {
  const config = loadIndex(cwd, env).config;
  return config.integrationConsent === true && isSetupAuthored(config, "integrationConsent");
}

/**
 * Persist integrationMode via setup. For direct, also records integrationConsent.
 * Does not touch runMode.
 * @param {string} cwd
 * @param {string} mode
 * @returns {"direct"|"worktree"|"auto"|"review"|null} null when mode invalid
 */
export function setIntegrationMode(cwd, mode, env = process.env) {
  const parsed = parseIntegrationMode(mode);
  if (!parsed) {
    return null;
  }
  const index = loadIndex(cwd, env);
  if (!index.config.prefsSources || typeof index.config.prefsSources !== "object") {
    index.config.prefsSources = {};
  }
  index.config.integrationMode = parsed;
  index.config.prefsSources.integrationMode = "setup";
  if (parsed === "direct") {
    index.config.integrationConsent = true;
    index.config.prefsSources.integrationConsent = "setup";
  }
  saveIndex(cwd, index, env);
  return parsed;
}

/**
 * One-screen refuse when effective integration is direct without setup consent.
 * When the resolved target workspace differs from companion cwd, the accept
 * command includes `--target <workspace>` so consent is recorded for the repo
 * that will be edited (not the companion cwd).
 *
 * @param {{ targetWorkspace?: string, companionCwd?: string }} [opts]
 * @returns {string}
 */
export function formatDirectIntegrationConsentMsg(opts = {}) {
  const targetWorkspace =
    opts.targetWorkspace != null && String(opts.targetWorkspace).trim() !== ""
      ? path.resolve(String(opts.targetWorkspace))
      : null;
  const companionCwd =
    opts.companionCwd != null && String(opts.companionCwd).trim() !== ""
      ? path.resolve(String(opts.companionCwd))
      : null;
  let targetFlag = "";
  if (targetWorkspace && companionCwd) {
    const cwdWorkspace = resolveTargetWorkspaceRoot(companionCwd, ".");
    if (path.resolve(targetWorkspace) !== path.resolve(cwdWorkspace)) {
      targetFlag = ` --target ${targetWorkspace}`;
    }
  } else if (targetWorkspace && !companionCwd) {
    targetFlag = ` --target ${targetWorkspace}`;
  }
  const targetLine = targetWorkspace
    ? ` Target workspace: ${targetWorkspace}.`
    : "";
  return (
    "Direct integration is the consented landing default: one-shot code edits " +
    "THIS working tree live (no worktree isolation, no pre-apply review); ACP " +
    "peer always uses an external worktree and applies a verified ready patch " +
    "only at peer-stop. Protected paths (.git config/HEAD/hooks/refs, .env, and " +
    "key files) are detected and rolled back if touched on code-direct live " +
    "edits." +
    targetLine +
    " To accept and make direct the default here: /grok:setup --integration direct" +
    targetFlag +
    " (or: companion setup --integration direct" +
    targetFlag +
    "). Or run this once with --integration worktree (isolated) or --integration review."
  );
}

/** Default (cwd-scoped) refuse copy; prefer formatDirectIntegrationConsentMsg for gates. */
export const DIRECT_INTEGRATION_CONSENT_MSG = formatDirectIntegrationConsentMsg();

/**
 * Drop any existing --integration flag(s) then append the resolved effective mode.
 * Ensures the wrapper never silently defaults behind the companion gate.
 * @param {string[]} args
 * @param {string} mode
 * @returns {string[]}
 */
export function withExplicitIntegration(args, mode) {
  const out = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--integration" && args[i + 1] !== undefined) {
      i += 1;
      continue;
    }
    if (typeof a === "string" && a.startsWith("--integration=")) {
      continue;
    }
    out.push(a);
  }
  out.push("--integration", mode);
  return out;
}

/**
 * Resolve effective integration for code/implement and enforce the one-time
 * direct consent gate. Consent and integrationMode are keyed on the resolved
 * TARGET repo root (git toplevel of --target, defaulting to '.'), not companion
 * cwd - so consent for repo A never authorizes a direct run against repo B.
 * worktree|auto|review need no consent; direct needs setup for that target.
 * @returns {{ ok: true, effective: string|null, rest: string[] } | { ok: false, code: number, message: string }}
 */
export function gateIntegrationForCodeish(mode, rest, integrationFlag, cwd, env = process.env) {
  if (mode !== "code" && mode !== "implement") {
    return { ok: true, rest, effective: null };
  }
  // continue-run is worktree-only in the wrapper (it loads the retained
  // worktree lineage and forbids --target/--base/--contract-file), so it never
  // does a live direct edit - exempt it from direct-integration consent, which
  // would otherwise refuse the documented continuation command in a fresh
  // workspace where the default mode resolves to direct.
  if (
    rest.includes("--continue-run") ||
    rest.some((a) => typeof a === "string" && a.startsWith("--continue-run="))
  ) {
    return { ok: true, rest, effective: null };
  }
  // implement is verify-only (code + handoff, never applies to the live tree),
  // so it ALWAYS takes the worktree path: never direct (which would record the
  // code leg as mode=direct and make the immediate handoff refuse it after
  // mutating the tree) and never the direct-consent gate.
  if (mode === "implement") {
    return { ok: true, effective: "worktree", rest: withExplicitIntegration(rest, "worktree") };
  }
  // SECURITY: key consent on the repo being edited, not process.cwd().
  const targetArg = parseTargetFlag(rest);
  const targetWorkspace = resolveTargetWorkspaceRoot(cwd, targetArg);
  let effective;
  if (integrationFlag != null && String(integrationFlag).trim() !== "") {
    const parsed = parseIntegrationMode(integrationFlag);
    if (!parsed) {
      return {
        ok: false,
        code: 1,
        message:
          `[grok-companion] invalid --integration ${JSON.stringify(integrationFlag)} ` +
          `(valid: direct|worktree|auto|review)\n`,
      };
    }
    effective = parsed;
  } else {
    effective = getIntegrationMode(targetWorkspace, env);
  }
  // auto/review: treat like worktree for gating (no live unverified tree writes).
  if (effective === "direct" && !getIntegrationConsent(targetWorkspace, env)) {
    return {
      ok: false,
      code: 1,
      message:
        formatDirectIntegrationConsentMsg({
          targetWorkspace,
          companionCwd: cwd,
        }) + "\n",
    };
  }
  return {
    ok: true,
    effective,
    rest: withExplicitIntegration(rest, effective),
  };
}

/**
 * Effective notification prefs.
 * Precedence per field: setup > CLAUDE_PLUGIN_OPTION_* env > built-in default.
 * @returns {{ notificationMode: string, notificationWebhookUrl: string|null }}
 */
export function getNotificationConfig(cwd, env = process.env) {
  const config = loadIndex(cwd, env).config;

  let notificationMode = DEFAULT_JOBS_CONFIG.notificationMode;
  if (isSetupAuthored(config, "notificationMode")) {
    notificationMode = config.notificationMode;
  } else {
    const opt = readPluginOption(env, PLUGIN_OPTION_NOTIFICATIONMODE_KEYS);
    if (opt) {
      const mode = parseNotificationMode(opt.value);
      if (mode) {
        notificationMode = mode;
      } else {
        noteInvalidPluginOption(opt.name, opt.value);
      }
    }
  }

  let notificationWebhookUrl = DEFAULT_JOBS_CONFIG.notificationWebhookUrl;
  if (isSetupAuthored(config, "notificationWebhookUrl")) {
    notificationWebhookUrl = config.notificationWebhookUrl;
  } else {
    const opt = readPluginOption(env, PLUGIN_OPTION_WEBHOOK_KEYS);
    if (opt) {
      const parsed = parseWebhookUrl(opt.value);
      if (parsed.ok) {
        notificationWebhookUrl = parsed.url;
      } else {
        noteInvalidPluginOption(opt.name, opt.value);
      }
    }
  }

  return { notificationMode, notificationWebhookUrl };
}

/**
 * @param {string} cwd
 * @param {{ notificationMode?: string, notificationWebhookUrl?: string|null }} patch
 */
export function setNotificationConfig(cwd, patch, env = process.env) {
  const index = loadIndex(cwd, env);
  if (!index.config.prefsSources || typeof index.config.prefsSources !== "object") {
    index.config.prefsSources = {};
  }
  if (patch.notificationMode !== undefined) {
    // Invalid modes leave prior prefs unchanged (never clobber auto -> off).
    const mode = parseNotificationMode(patch.notificationMode);
    if (mode) {
      index.config.notificationMode = mode;
      index.config.prefsSources.notificationMode = "setup";
    }
  }
  if (patch.notificationWebhookUrl !== undefined) {
    // Invalid non-empty URLs leave prior webhook unchanged; empty clears.
    const parsed = parseWebhookUrl(patch.notificationWebhookUrl);
    if (parsed.ok) {
      index.config.notificationWebhookUrl = parsed.url;
      index.config.prefsSources.notificationWebhookUrl = "setup";
    }
  }
  saveIndex(cwd, index, env);
  return getNotificationConfig(cwd, env);
}

export function mintJobId() {
  const ts = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  return `${ts}-${randomBytes(3).toString("hex")}`;
}

export function jobPaths(cwd, jobId, env = process.env) {
  assertJobIdSafe(jobId);
  const root = jobsDir(cwd, env);
  const dir = path.resolve(root, jobId);
  if (!dir.startsWith(root + path.sep) && dir !== root) {
    throw new Error(`job path escapes jobs dir: ${jobId}`);
  }
  return {
    dir,
    meta: path.join(dir, "job.json"),
    log: path.join(dir, "job.log"),
    stdout: path.join(dir, "stdout.json"),
  };
}

export function createJob(cwd, partial, env = process.env) {
  const id = partial.id || mintJobId();
  const paths = jobPaths(cwd, id, env);
  mkdirPrivate(paths.dir);
  const job = {
    id,
    kind: partial.kind || "run",
    mode: partial.mode || null,
    status: "running",
    runMode: partial.runMode || getRunMode(cwd, env),
    pid: partial.pid ?? null,
    pgid: partial.pgid ?? null,
    runId: partial.runId ?? null,
    createdAt: nowIso(),
    updatedAt: nowIso(),
    summary: partial.summary ?? null,
    error: null,
  };
  writePrivate(paths.meta, `${JSON.stringify(job, null, 2)}\n`);
  writePrivate(paths.log, `[${job.createdAt}] start ${job.kind} mode=${job.mode}\n`);
  const index = loadIndex(cwd, env);
  index.jobs = [job, ...index.jobs.filter((j) => j.id !== id)];
  if (job.kind === "rescue") {
    index.config.lastRescueJobId = id;
  }
  saveIndex(cwd, index, env);
  return job;
}

export function updateJob(cwd, jobId, patch, env = process.env) {
  const paths = jobPaths(cwd, jobId, env);
  let job = { id: jobId };
  if (fs.existsSync(paths.meta)) {
    try {
      job = JSON.parse(fs.readFileSync(paths.meta, "utf8"));
    } catch {
      job = { id: jobId };
    }
  }
  job = { ...job, ...patch, id: jobId, updatedAt: nowIso() };
  mkdirPrivate(paths.dir);
  writePrivate(paths.meta, `${JSON.stringify(job, null, 2)}\n`);
  const index = loadIndex(cwd, env);
  index.jobs = [job, ...index.jobs.filter((j) => j.id !== jobId)];
  saveIndex(cwd, index, env);
  return job;
}

export function appendJobLog(cwd, jobId, line, env = process.env) {
  const paths = jobPaths(cwd, jobId, env);
  mkdirPrivate(paths.dir);
  fs.appendFileSync(paths.log, `[${nowIso()}] ${line}\n`, { encoding: "utf8", mode: FILE_MODE });
}

export function storeJobStdout(cwd, jobId, text, env = process.env) {
  const paths = jobPaths(cwd, jobId, env);
  mkdirPrivate(paths.dir);
  writePrivate(paths.stdout, text);
}

export function listJobs(cwd, env = process.env) {
  return loadIndex(cwd, env).jobs;
}

export function getJob(cwd, jobId, env = process.env) {
  if (!jobId) {
    const jobs = listJobs(cwd, env);
    return jobs[0] ?? null;
  }
  if (!isValidJobId(jobId)) {
    return null;
  }
  const paths = jobPaths(cwd, jobId, env);
  if (fs.existsSync(paths.meta)) {
    try {
      return JSON.parse(fs.readFileSync(paths.meta, "utf8"));
    } catch {
      return null;
    }
  }
  return listJobs(cwd, env).find((j) => j.id === jobId) ?? null;
}

/**
 * Resolve a job by its stored wrapper/direct runId (newest-first index order).
 * @returns {object|null}
 */
export function findJobByRunId(cwd, runId, env = process.env) {
  if (!runId) return null;
  const jobs = listJobs(cwd, env); // newest-first ordering already used by the table
  return jobs.find((j) => j.runId === runId) || null;
}

/**
 * Resolve a job from a positional that may be a job id or a runId.
 * Same id shape (JOB_ID_RE / RUN_ID_RE); exact job-id match wins, then runId.
 * Collision: job A id === job B runId returns A (getJob), not B.
 * @returns {object|null}
 */
export function resolveJobByIdOrRunId(cwd, idOrRunId, env = process.env) {
  // Prefer exact job-id match so a job id that collides with another job's
  // runId never resolves to the wrong record (shared YYYYMMDDTHHMMSSZ shape).
  let job = getJob(cwd, idOrRunId, env);
  if (!job && idOrRunId) {
    job = findJobByRunId(cwd, idOrRunId, env);
  }
  return job;
}

export function readJobStdout(cwd, jobId, env = process.env) {
  const paths = jobPaths(cwd, jobId, env);
  if (!fs.existsSync(paths.stdout)) {
    return null;
  }
  return fs.readFileSync(paths.stdout, "utf8");
}

export function getLastRescueJobId(cwd, env = process.env) {
  return loadIndex(cwd, env).config.lastRescueJobId ?? null;
}

export function formatJobsTable(jobs) {
  if (!jobs.length) {
    return "No Grok jobs recorded for this workspace yet.\n";
  }
  const header = ["ID", "KIND", "STATUS", "MODE", "RUN", "UPDATED"];
  const rows = jobs.map((j) => [
    j.id,
    j.kind ?? "",
    j.status ?? "",
    j.runMode ?? "",
    j.runId ?? "",
    (j.updatedAt ?? "").replace("T", " ").replace("Z", ""),
  ]);
  const widths = header.map((h, i) => Math.max(h.length, ...rows.map((r) => String(r[i]).length)));
  const fmt = (cells) => cells.map((c, i) => String(c).padEnd(widths[i])).join("  ");
  return [fmt(header), fmt(widths.map((w) => "-".repeat(w))), ...rows.map(fmt)].join("\n") + "\n";
}
