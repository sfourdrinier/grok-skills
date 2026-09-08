// plugin/scripts/lib/jobs.mjs
//
// Per-workspace job registry for Grok companion runs (status / result / cancel).
// Mirrors the codex-plugin job idea without depending on Codex. Plugin-local
// state only; safety still lives in the wrapper (hardened mode).

import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import {
  atomicWritePrivate,
  mkdirPrivate,
  withExclusiveLockSync,
} from "./atomic-file.mjs";
import { resolveTargetWorkspaceRoot } from "./git-context.mjs";
import {
  DURABLE_STATE_FALLBACK,
  jobsDir,
  LEGACY_TMP_STATE_FALLBACK,
  stateRoot,
} from "./jobs-layout.mjs";
import {
  isNotificationMode,
  NOTIFICATION_MODES,
  parseNotificationMode,
  parseWebhookUrl,
} from "./notification-modes.mjs";

export { isNotificationMode, NOTIFICATION_MODES, parseNotificationMode, parseWebhookUrl };
export { jobsDir, DURABLE_STATE_FALLBACK, LEGACY_TMP_STATE_FALLBACK };

const MAX_JOBS = 50;
const JOB_ID_RE = /^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$/;
const FILE_MODE = 0o600;
const PREFS_NAME = "prefs.json";
const PREFS_BAK_NAME = "prefs.json.bak";
const SCOPE_SIDECAR = "codex-agents-prefs.json";
const ACTIVE_JOB_STATUSES = new Set([
  "running",
  "cancel_requested",
  "cancel_failed",
  "cancel_unconfirmed",
]);

/** Single source of jobs-index config defaults (design §11). */
export const DEFAULT_JOBS_CONFIG = Object.freeze({
  runMode: "hardened",
  // Issue #8: recommended default for new installs (background completion signals).
  notificationMode: "auto",
  notificationWebhookUrl: null,
  lastRescueJobId: null,
  // Integration (how edits land) is orthogonal to runMode (security posture).
  integrationMode: "direct",
  // Legacy field kept in index for forward-compat reads; never gated (2.0.1+).
  integrationConsent: true,
  codexAgentsScope: "user",
});

// Pre-2.0.1 product default. Used only when classifying legacy indexes that
// lack prefsSources so a persisted "off" (old default, never setup) is not
// pinned as setup-authored after the default flipped to auto (Codex PR review).
const LEGACY_DEFAULT_NOTIFICATION_MODE = "off";

// Pre-prefsSources product default for integration. Same as today's default
// ("direct"), but used so a legacy index that stored worktree/auto/review
// (evidence of deliberate setup --integration) is pinned as setup-authored
// and is not silently downgraded to live-tree direct after consent removal
// (Codex PR #9).
const LEGACY_DEFAULT_INTEGRATION_MODE = "direct";

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
    // Compare notificationMode against the PRE-2.0.1 default ("off"), not the
    // current DEFAULT_JOBS_CONFIG.auto - otherwise every legacy "off" row is
    // wrongly pinned as setup-authored and ignores userConfig / new default.
    if (raw?.runMode === "direct") prefsSources.runMode = "setup";
    const storedNotify = parseNotificationMode(raw?.notificationMode);
    if (storedNotify && storedNotify !== LEGACY_DEFAULT_NOTIFICATION_MODE) {
      prefsSources.notificationMode = "setup";
    }
    if (normalizeWebhookUrl(raw?.notificationWebhookUrl)) {
      prefsSources.notificationWebhookUrl = "setup";
    }
    // Pin non-default legacy integrationMode (worktree/auto/review). A stored
    // "direct" matches the old default and is left unpinned so userConfig /
    // built-in default still apply; deliberate setup --integration worktree
    // must survive consent removal without falling through to live-tree edits.
    const storedIntegration = parseIntegrationMode(raw?.integrationMode);
    if (storedIntegration && storedIntegration !== LEGACY_DEFAULT_INTEGRATION_MODE) {
      prefsSources.integrationMode = "setup";
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
    codexAgentsScope: parseCodexAgentsScope(raw?.codexAgentsScope) ?? DEFAULT_JOBS_CONFIG.codexAgentsScope,
    prefsSources,
  };
}

/** @param {unknown} value @returns {"user"|"project"|null} */
export function parseCodexAgentsScope(value) {
  const s = String(value ?? "")
    .trim()
    .toLowerCase();
  return s === "user" || s === "project" ? s : null;
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

function writePrivate(filePath, content) {
  atomicWritePrivate(filePath, content);
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

function indexPath(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), "jobs-index.json");
}

function prefsPath(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), PREFS_NAME);
}

function prefsBakPath(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), PREFS_BAK_NAME);
}

function prefsLockPath(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), "prefs.lock");
}

function sidecarScopePath(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), SCOPE_SIDECAR);
}

function ensure(cwd, env = process.env) {
  mkdirPrivate(stateRoot(cwd, env));
  mkdirPrivate(jobsDir(cwd, env));
}

function readJsonObject(file) {
  if (!fs.existsSync(file)) return null;
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function readSidecarScope(cwd, env) {
  const parsed = readJsonObject(sidecarScopePath(cwd, env));
  return parseCodexAgentsScope(parsed?.codexAgentsScope);
}

function prefsConfigUsable(config) {
  if (!config || typeof config !== "object" || Array.isArray(config)) return false;
  const hasPolicyField =
    config.integrationMode != null ||
    config.runMode != null ||
    (config.prefsSources && typeof config.prefsSources === "object");
  if (!hasPolicyField) return false;
  const rawMode = config.integrationMode;
  if (rawMode != null && String(rawMode).trim() !== "" && !parseIntegrationMode(rawMode)) {
    return false;
  }
  if (config.prefsSources?.integrationMode === "setup" && !parseIntegrationMode(rawMode)) {
    return false;
  }
  return true;
}

function failClosedConfig() {
  return normalizeConfig({
    ...DEFAULT_JOBS_CONFIG,
    integrationMode: "review",
    prefsSources: { integrationMode: "setup" },
  });
}

function prefsStoreExists(cwd, env = process.env) {
  return fs.existsSync(prefsPath(cwd, env)) || fs.existsSync(prefsBakPath(cwd, env));
}

function copyPrefsBak(src, dest) {
  try {
    fs.copyFileSync(src, dest);
    try {
      fs.chmodSync(dest, FILE_MODE);
    } catch {
      /* best-effort */
    }
  } catch {
    /* bak is best-effort */
  }
}

function persistPrefs(cwd, config, env = process.env) {
  const file = prefsPath(cwd, env);
  const bak = prefsBakPath(cwd, env);
  const payload = `${JSON.stringify({ version: 2, config }, null, 2)}\n`;
  atomicWritePrivate(file, payload);
  if (prefsConfigUsable(config)) {
    copyPrefsBak(file, bak);
  }
  const existing = readJsonObject(indexPath(cwd, env)) || { version: 1, jobs: [] };
  const snapshot = {
    version: 1,
    config: {
      runMode: config.runMode,
      notificationMode: config.notificationMode,
      notificationWebhookUrl: config.notificationWebhookUrl,
      lastRescueJobId: config.lastRescueJobId,
      integrationMode: config.integrationMode,
      integrationConsent: config.integrationConsent === true,
      codexAgentsScope: config.codexAgentsScope ?? DEFAULT_JOBS_CONFIG.codexAgentsScope,
      prefsSources: config.prefsSources ?? {},
    },
    jobs: Array.isArray(existing.jobs) ? existing.jobs : [],
  };
  writePrivate(indexPath(cwd, env), `${JSON.stringify(snapshot, null, 2)}\n`);
}

function loadPrefsConfig(cwd, env = process.env) {
  const fromPrefs = readJsonObject(prefsPath(cwd, env));
  if (prefsConfigUsable(fromPrefs?.config)) {
    return normalizeConfig(fromPrefs.config);
  }
  const fromBak = readJsonObject(prefsBakPath(cwd, env));
  if (prefsConfigUsable(fromBak?.config)) {
    return normalizeConfig(fromBak.config);
  }
  if (prefsStoreExists(cwd, env)) {
    return failClosedConfig();
  }
  return null;
}

function loadEffectivePrefs(cwd, env = process.env) {
  ensure(cwd, env);
  return loadPrefsConfig(cwd, env) ?? migrateLegacyIndexConfig(cwd, env) ?? emptyConfig();
}

function mutatePrefs(cwd, patchFn, env = process.env) {
  ensure(cwd, env);
  return withExclusiveLockSync(prefsLockPath(cwd, env), () => {
    const latest = loadPrefsConfig(cwd, env) ?? migrateLegacyIndexConfig(cwd, env) ?? emptyConfig();
    const next = patchFn({
      ...latest,
      prefsSources:
        latest.prefsSources && typeof latest.prefsSources === "object"
          ? { ...latest.prefsSources }
          : {},
    });
    const normalized = normalizeConfig(next);
    persistPrefs(cwd, normalized, env);
    return normalized;
  });
}

function writeJobListing(cwd, env = process.env) {
  ensure(cwd, env);
  const jobs = listingFromRecords(readAllJobRecords(cwd, env));
  const existing = readJsonObject(indexPath(cwd, env)) || { version: 1 };
  const payload = {
    version: 1,
    config: existing.config ?? {},
    jobs,
  };
  writePrivate(indexPath(cwd, env), `${JSON.stringify(payload, null, 2)}\n`);
  return jobs;
}

function emptyConfig() {
  return normalizeConfig({ ...DEFAULT_JOBS_CONFIG, prefsSources: {} });
}

function readAllJobRecords(cwd, env = process.env) {
  const dir = jobsDir(cwd, env);
  if (!fs.existsSync(dir)) return [];
  const jobs = [];
  let names = [];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  for (const name of names) {
    if (!isValidJobId(name)) continue;
    const meta = path.join(dir, name, "job.json");
    const parsed = readJsonObject(meta);
    if (parsed && parsed.id) jobs.push(parsed);
  }
  jobs.sort((a, b) => String(b.updatedAt ?? "").localeCompare(String(a.updatedAt ?? "")));
  return jobs;
}

function listingFromRecords(jobs) {
  const active = jobs.filter((j) => ACTIVE_JOB_STATUSES.has(j.status));
  const finished = jobs.filter((j) => !ACTIVE_JOB_STATUSES.has(j.status)).slice(0, MAX_JOBS);
  return [...active, ...finished].sort((a, b) =>
    String(b.updatedAt ?? "").localeCompare(String(a.updatedAt ?? ""))
  );
}

function migrateLegacyIndexConfig(cwd, env) {
  const parsed = readJsonObject(indexPath(cwd, env));
  if (!parsed || typeof parsed !== "object") return null;
  const legacySetup =
    parsed.config != null &&
    (parsed.config.prefsSources === undefined || parsed.config.prefsSources === null);
  const config = normalizeConfig(parsed.config, { legacySetup });
  const sidecarScope = readSidecarScope(cwd, env);
  if (sidecarScope && !isSetupAuthored(config, "codexAgentsScope")) {
    config.codexAgentsScope = sidecarScope;
    if (sidecarScope !== DEFAULT_JOBS_CONFIG.codexAgentsScope) {
      config.prefsSources = { ...(config.prefsSources ?? {}), codexAgentsScope: "setup" };
    }
  }
  return config;
}

function loadIndex(cwd, env = process.env) {
  ensure(cwd, env);
  let config = loadPrefsConfig(cwd, env);
  if (!config) {
    config = migrateLegacyIndexConfig(cwd, env) ?? emptyConfig();
    persistPrefs(cwd, config, env);
  }
  const jobs = listingFromRecords(readAllJobRecords(cwd, env));
  return { version: 1, jobs, config };
}

function saveIndex(cwd, index, env = process.env) {
  ensure(cwd, env);
  const config = normalizeConfig(index.config);
  return withExclusiveLockSync(prefsLockPath(cwd, env), () => {
    persistPrefs(cwd, config, env);
    const jobs = listingFromRecords(readAllJobRecords(cwd, env));
    const payload = {
      version: 1,
      config: {
        runMode: config.runMode,
        notificationMode: config.notificationMode,
        notificationWebhookUrl: config.notificationWebhookUrl,
        lastRescueJobId: config.lastRescueJobId,
        integrationMode: config.integrationMode,
        integrationConsent: config.integrationConsent === true,
        codexAgentsScope: config.codexAgentsScope ?? DEFAULT_JOBS_CONFIG.codexAgentsScope,
        prefsSources: config.prefsSources ?? {},
      },
      jobs,
    };
    writePrivate(indexPath(cwd, env), `${JSON.stringify(payload, null, 2)}\n`);
    return payload;
  });
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
  const config = loadEffectivePrefs(cwd, env);
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
  const config = mutatePrefs(
    cwd,
    (current) => {
      current.runMode = mode === "direct" ? "direct" : "hardened";
      current.prefsSources.runMode = "setup";
      return current;
    },
    env
  );
  return config.runMode;
}

/**
 * Effective integration mode (how edits land: direct|worktree|auto|review).
 * Precedence: setup prefs > CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE > default.
 * Orthogonal to runMode. Env alone is a default hint, never consent.
 * @returns {"direct"|"worktree"|"auto"|"review"}
 */
export function getIntegrationMode(cwd, env = process.env) {
  const config = loadEffectivePrefs(cwd, env);
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
 * Legacy no-op: consent gates were removed in 2.0.1 (always allowed).
 * Kept so older importers/tests do not crash; do not reintroduce gating.
 * @returns {true}
 */
export function getIntegrationConsent(_cwd, _env = process.env) {
  return true;
}

/**
 * Persist integrationMode via setup. Does not touch runMode.
 * @param {string} cwd
 * @param {string} mode
 * @returns {"direct"|"worktree"|"auto"|"review"|null} null when mode invalid
 */
export function setIntegrationMode(cwd, mode, env = process.env) {
  const parsed = parseIntegrationMode(mode);
  if (!parsed) {
    return null;
  }
  mutatePrefs(
    cwd,
    (current) => {
      current.integrationMode = parsed;
      current.prefsSources.integrationMode = "setup";
      current.integrationConsent = true;
      current.prefsSources.integrationConsent = "setup";
      return current;
    },
    env
  );
  return parsed;
}

/**
 * @deprecated Consent gates removed in 2.0.1. Kept for import compatibility.
 * @returns {string}
 */
export function formatDirectIntegrationConsentMsg(_opts = {}) {
  return (
    "Direct integration is the product default: one-shot code edits THIS working " +
    "tree live (no worktree isolation, no pre-apply review); ACP peer always uses " +
    "an external worktree and applies a verified ready patch only at peer-stop. " +
    "Protected paths (.git config/HEAD/hooks/refs, .env, and key files) are " +
    "detected and rolled back if touched on code-direct live edits. " +
    "Set default with: /grok:setup --integration direct|worktree|auto|review."
  );
}

/** @deprecated Consent gates removed in 2.0.1. */
export const DIRECT_INTEGRATION_CONSENT_MSG = formatDirectIntegrationConsentMsg();

// Integration gate + continue-run target live in integration-gate.mjs (900-line
// cap). Re-export for existing import paths (jobs.mjs remains the public surface).
export {
  gateIntegrationForCodeish,
  resolveContinueRunTargetWorkspace,
  withExplicitIntegration,
} from "./integration-gate.mjs";

/**
 * Effective notification prefs.
 * Precedence per field: setup > CLAUDE_PLUGIN_OPTION_* env > built-in default.
 * @returns {{ notificationMode: string, notificationWebhookUrl: string|null }}
 */
export function getNotificationConfig(cwd, env = process.env) {
  const config = loadEffectivePrefs(cwd, env);

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
  mutatePrefs(
    cwd,
    (current) => {
      if (patch.notificationMode !== undefined) {
        const mode = parseNotificationMode(patch.notificationMode);
        if (mode) {
          current.notificationMode = mode;
          current.prefsSources.notificationMode = "setup";
        }
      }
      if (patch.notificationWebhookUrl !== undefined) {
        const parsed = parseWebhookUrl(patch.notificationWebhookUrl);
        if (parsed.ok) {
          current.notificationWebhookUrl = parsed.url;
          current.prefsSources.notificationWebhookUrl = "setup";
        }
      }
      return current;
    },
    env
  );
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
    pgidKind: partial.pgidKind ?? "none",
    startId: partial.startId ?? null,
    runId: partial.runId ?? null,
    createdAt: nowIso(),
    updatedAt: nowIso(),
    summary: partial.summary ?? null,
    error: null,
  };
  writePrivate(paths.meta, `${JSON.stringify(job, null, 2)}\n`);
  writePrivate(paths.log, `[${job.createdAt}] start ${job.kind} mode=${job.mode}\n`);
  if (job.kind === "rescue") {
    mutatePrefs(
      cwd,
      (current) => {
        current.lastRescueJobId = id;
        return current;
      },
      env
    );
  }
  writeJobListing(cwd, env);
  return job;
}

export function updateJob(cwd, jobId, patch, env = process.env) {
  const paths = jobPaths(cwd, jobId, env);
  mkdirPrivate(paths.dir);
  const job = withExclusiveLockSync(path.join(paths.dir, "job.lock"), () => {
    let current = { id: jobId };
    if (fs.existsSync(paths.meta)) {
      try {
        current = JSON.parse(fs.readFileSync(paths.meta, "utf8"));
      } catch {
        current = { id: jobId };
      }
    }
    const nextPatch = { ...patch };
    if (
      current.status === "cancelled" &&
      (nextPatch.status === "success" || nextPatch.status === "failure")
    ) {
      delete nextPatch.status;
    }
    const merged = { ...current, ...nextPatch, id: jobId, updatedAt: nowIso() };
    writePrivate(paths.meta, `${JSON.stringify(merged, null, 2)}\n`);
    return merged;
  });
  writeJobListing(cwd, env);
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
  ensure(cwd, env);
  return listingFromRecords(readAllJobRecords(cwd, env));
}

export function getStoredCodexAgentsScope(cwd, env = process.env) {
  const config = loadEffectivePrefs(cwd, env);
  return parseCodexAgentsScope(config.codexAgentsScope) ?? DEFAULT_JOBS_CONFIG.codexAgentsScope;
}

export function setStoredCodexAgentsScope(cwd, scope, env = process.env) {
  const normalized = parseCodexAgentsScope(scope) || DEFAULT_JOBS_CONFIG.codexAgentsScope;
  mutatePrefs(
    cwd,
    (current) => {
      current.codexAgentsScope = normalized;
      current.prefsSources.codexAgentsScope = "setup";
      return current;
    },
    env
  );
  return normalized;
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
  return loadEffectivePrefs(cwd, env).lastRescueJobId ?? null;
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
