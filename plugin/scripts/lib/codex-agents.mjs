// plugin/scripts/lib/codex-agents.mjs
//
// Install Codex custom-agent TOML templates shipped under plugin/codex-agents/
// into ~/.codex/agents/ (or project .codex/agents when scope=project).
//
// Codex does not yet register plugin-bundled agents (openai/codex#18988), so we
// materialize them into the agents dir. Prefer ensureCodexAgents() from
// SessionStart so install is zero-step for the user. Templates use
// __GROK_AGENT_RUN_Q__; install rewrites an absolute path to agents/run.mjs.
//
// Project-scope agent discovery (.codex/agents/) per Codex docs July 2026:
// https://developers.openai.com/codex/subagents
// (personal ~/.codex/agents, project .codex/agents).

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { getRunMode, jobsDir } from "./jobs.mjs";

const DIR_MODE = 0o700;
const FILE_MODE = 0o600;
const MANAGED_BY = "grok-skills";
const AGENT_RUN_PLACEHOLDER = "__GROK_AGENT_RUN_Q__";
/** @deprecated legacy templates */
const COMPANION_PLACEHOLDER = "__GROK_COMPANION_Q__";
const MANAGED_NAME_PREFIX = "grok-";
/** Keep at most this many managed-agent *.bak* files per target (newest first). */
export const MAX_MANAGED_AGENT_BACKUPS = 3;
const CODEX_AGENTS_SCOPE_KEY = "codexAgentsScope";
const SCOPE_SIDECAR = "codex-agents-prefs.json";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_TEMPLATES_DIR = path.resolve(SCRIPT_DIR, "..", "..", "codex-agents");
const DEFAULT_PLUGIN_ROOT = path.resolve(SCRIPT_DIR, "..", "..");

export function codexHome(env = process.env) {
  const fromEnv = (env.CODEX_HOME ?? "").trim();
  if (fromEnv) {
    return path.resolve(fromEnv);
  }
  return path.join(os.homedir(), ".codex");
}

export function codexAgentsDir(env = process.env) {
  return path.join(codexHome(env), "agents");
}

/**
 * @param {unknown} value
 * @returns {"user"|"project"|null}
 */
export function parseCodexAgentsScope(value) {
  const s = String(value ?? "")
    .trim()
    .toLowerCase();
  if (s === "user" || s === "project") {
    return s;
  }
  return null;
}

function stateRootFromJobs(cwd, env) {
  // jobsDir -> <stateRoot>/jobs; ensure() side effect via getRunMode load path.
  getRunMode(cwd, env);
  return path.dirname(jobsDir(cwd, env));
}

function jobsIndexPath(cwd, env) {
  return path.join(stateRootFromJobs(cwd, env), "jobs-index.json");
}

function scopeSidecarPath(cwd, env) {
  return path.join(stateRootFromJobs(cwd, env), SCOPE_SIDECAR);
}

function readScopeFromIndex(cwd, env) {
  try {
    const file = jobsIndexPath(cwd, env);
    if (!fs.existsSync(file)) {
      return null;
    }
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    return parseCodexAgentsScope(parsed?.config?.[CODEX_AGENTS_SCOPE_KEY]);
  } catch {
    return null;
  }
}

function readScopeFromSidecar(cwd, env) {
  try {
    const file = scopeSidecarPath(cwd, env);
    if (!fs.existsSync(file)) {
      return null;
    }
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    return parseCodexAgentsScope(parsed?.codexAgentsScope);
  } catch {
    return null;
  }
}

/**
 * Effective Codex agents install scope for this workspace.
 * Default `user` = personal ~/.codex/agents (historical behavior).
 * Persisted in workspace prefs (jobs-index.json) alongside run mode; a small
 * sidecar re-hydrates the value if jobs-index rewrite drops unknown keys.
 *
 * @param {string} cwd
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {"user"|"project"}
 */
export function getCodexAgentsScope(cwd, env = process.env) {
  return readScopeFromIndex(cwd, env) || readScopeFromSidecar(cwd, env) || "user";
}

/**
 * Persist Codex agents install scope in workspace prefs (same jobs-index as
 * run mode) plus a resilience sidecar under the same state root.
 *
 * @param {string} cwd
 * @param {string} scope
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {"user"|"project"}
 */
export function setCodexAgentsScope(cwd, scope, env = process.env) {
  const normalized = parseCodexAgentsScope(scope) || "user";
  const root = stateRootFromJobs(cwd, env);
  fs.mkdirSync(root, { recursive: true, mode: DIR_MODE });

  const indexFile = path.join(root, "jobs-index.json");
  let payload = { version: 1, jobs: [], config: {} };
  if (fs.existsSync(indexFile)) {
    try {
      const parsed = JSON.parse(fs.readFileSync(indexFile, "utf8"));
      if (parsed && typeof parsed === "object") {
        payload = parsed;
      }
    } catch {
      /* start fresh structure but keep going */
    }
  }
  if (!payload.config || typeof payload.config !== "object") {
    payload.config = {};
  }
  payload.config[CODEX_AGENTS_SCOPE_KEY] = normalized;
  if (!payload.config.prefsSources || typeof payload.config.prefsSources !== "object") {
    payload.config.prefsSources = {};
  }
  payload.config.prefsSources[CODEX_AGENTS_SCOPE_KEY] = "setup";
  if (!Array.isArray(payload.jobs)) {
    payload.jobs = [];
  }
  if (payload.version == null) {
    payload.version = 1;
  }
  fs.writeFileSync(indexFile, `${JSON.stringify(payload, null, 2)}\n`, {
    encoding: "utf8",
    mode: FILE_MODE,
  });

  // Sidecar: jobs.mjs saveIndex only re-emits known config keys and would drop
  // codexAgentsScope; keep a same-state-root copy so SessionStart still honors
  // project scope after unrelated prefs/job writes.
  fs.writeFileSync(
    path.join(root, SCOPE_SIDECAR),
    `${JSON.stringify({ codexAgentsScope: normalized }, null, 2)}\n`,
    { encoding: "utf8", mode: FILE_MODE }
  );
  return normalized;
}

/**
 * Resolve the Codex agents destination directory for install/ensure.
 * Project scope -> <cwd>/.codex/agents; user scope -> ~/.codex/agents (or CODEX_HOME).
 *
 * Project-scope agent discovery (.codex/agents/) per Codex docs July 2026:
 * https://developers.openai.com/codex/subagents
 * (personal ~/.codex/agents, project .codex/agents).
 *
 * @param {{ cwd?: string, env?: NodeJS.ProcessEnv, scope?: string|null }} [opts]
 * @returns {string}
 */
export function resolveCodexAgentsDestDir({
  cwd = process.cwd(),
  env = process.env,
  scope = null,
} = {}) {
  const resolved =
    parseCodexAgentsScope(scope) || getCodexAgentsScope(cwd, env) || "user";
  if (resolved === "project") {
    return path.join(path.resolve(cwd), ".codex", "agents");
  }
  return codexAgentsDir(env);
}

export function listTemplateAgents(templatesDir = DEFAULT_TEMPLATES_DIR) {
  if (!fs.existsSync(templatesDir)) {
    return [];
  }
  return fs
    .readdirSync(templatesDir)
    .filter((name) => name.endsWith(".toml") && !name.includes(path.sep) && !name.includes("/"))
    .map((name) => ({
      name: name.replace(/\.toml$/, ""),
      source: path.join(templatesDir, name),
    }))
    .filter((t) => t.name && !t.name.includes("..") && !t.name.includes(path.sep))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export function resolveCompanionPath(pluginRoot = DEFAULT_PLUGIN_ROOT) {
  return path.resolve(pluginRoot, "scripts", "grok-companion.mjs");
}

export function resolveAgentRunPath(pluginRoot = DEFAULT_PLUGIN_ROOT) {
  return path.resolve(pluginRoot, "agents", "run.mjs");
}

/** POSIX single-quote for embedding absolute paths in agent shell recipes. */
export function shellSingleQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

export function isManagedAgentBody(body) {
  return /(?:^|\n)#\s*managed-by:\s*grok-skills\b/m.test(String(body || ""));
}

function templateSha(sourceBody) {
  return crypto.createHash("sha256").update(sourceBody, "utf8").digest("hex").slice(0, 16);
}

/**
 * Build the installed TOML body: managed header + absolute agents/run.mjs path.
 * @param {string} sourceBody
 * @param {string} agentRunAbs - absolute path to agents/run.mjs
 * @param {string} [companionAbs] - optional companion path for header metadata
 */
export function materializeAgentBody(sourceBody, agentRunAbs, companionAbs = null) {
  const quoted = shellSingleQuote(agentRunAbs);
  let rewritten = String(sourceBody);
  if (rewritten.includes(AGENT_RUN_PLACEHOLDER)) {
    rewritten = rewritten.split(AGENT_RUN_PLACEHOLDER).join(quoted);
  } else if (rewritten.includes(COMPANION_PLACEHOLDER)) {
    // Legacy templates pointed at companion; rewrite to agent runner instead.
    rewritten = rewritten.split(COMPANION_PLACEHOLDER).join(quoted);
  } else {
    throw new Error(
      `template missing ${AGENT_RUN_PLACEHOLDER} placeholder (refusing to install without absolute agent runner path)`
    );
  }
  const sha = templateSha(sourceBody);
  const header = [
    `# managed-by: ${MANAGED_BY}`,
    `# agent-run: ${agentRunAbs}`,
    companionAbs ? `# companion: ${companionAbs}` : null,
    `# template-sha256: ${sha}`,
    `# auto-installed by SessionStart / setup - re-runs update managed agents only`,
    "",
  ]
    .filter(Boolean)
    .join("\n");
  const withoutOldInstallComments = rewritten.replace(/^(?:#.*\n)*?(?=name\s*=)/m, "");
  return header + withoutOldInstallComments;
}

function mkdirPrivate(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: DIR_MODE });
  try {
    fs.chmodSync(dir, DIR_MODE);
  } catch {
    /* best-effort */
  }
}

function writePrivate(filePath, content) {
  // Atomic replace: write temp then rename so a killed SessionStart cannot leave
  // a truncated ~/.codex/agents/grok-*.toml.
  const tmpPath = `${filePath}.tmp.${process.pid}`;
  fs.writeFileSync(tmpPath, content, { encoding: "utf8", mode: FILE_MODE });
  try {
    fs.chmodSync(tmpPath, FILE_MODE);
  } catch {
    /* best-effort */
  }
  fs.renameSync(tmpPath, filePath);
  try {
    fs.chmodSync(filePath, FILE_MODE);
  } catch {
    /* best-effort */
  }
}

/**
 * True when name is a backup sibling of baseName: base.bak or base.bak.N
 * @param {string} baseName
 * @param {string} name
 */
function isBackupNameFor(baseName, name) {
  if (name === `${baseName}.bak`) {
    return true;
  }
  return (
    name.startsWith(`${baseName}.bak.`) &&
    /^\d+$/.test(name.slice(`${baseName}.bak.`.length))
  );
}

/**
 * Managed-agent backup siblings of filePath (content must carry managed-by).
 * @param {string} filePath
 * @returns {{ path: string, mtimeMs: number, name: string }[]}
 */
export function listManagedAgentBackups(filePath) {
  const dir = path.dirname(filePath);
  const baseName = path.basename(filePath);
  if (!fs.existsSync(dir)) {
    return [];
  }
  let names;
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  const out = [];
  for (const name of names) {
    if (!isBackupNameFor(baseName, name)) {
      continue;
    }
    const full = path.join(dir, name);
    try {
      const body = fs.readFileSync(full, "utf8");
      if (!isManagedAgentBody(body)) {
        continue;
      }
      const st = fs.statSync(full);
      out.push({ path: full, mtimeMs: st.mtimeMs, name });
    } catch {
      /* skip unreadable */
    }
  }
  return out;
}

/**
 * Keep the newest `keep` managed backups for filePath; delete older managed ones.
 * Never touches files whose content lacks the managed-by header.
 * @param {string} filePath
 * @param {number} [keep]
 * @returns {{ kept: string[], deleted: string[] }}
 */
export function pruneManagedAgentBackups(filePath, keep = MAX_MANAGED_AGENT_BACKUPS) {
  const backups = listManagedAgentBackups(filePath).sort(
    (a, b) => b.mtimeMs - a.mtimeMs
  );
  const kept = backups.slice(0, keep);
  const deleted = [];
  for (const b of backups.slice(keep)) {
    try {
      fs.unlinkSync(b.path);
      deleted.push(b.path);
    } catch {
      /* best-effort */
    }
  }
  return { kept: kept.map((b) => b.path), deleted };
}

/**
 * Backup existing file to path.bak (and path.bak.N if needed). Returns backup path or null.
 * After writing a new backup, cap managed-agent backups at MAX_MANAGED_AGENT_BACKUPS
 * (newest 3 total including the new one). Only prunes backups whose content
 * carries the managed-by header - never touches user files.
 */
export function backupAgentFile(filePath) {
  if (!fs.existsSync(filePath)) {
    return null;
  }
  let backup = `${filePath}.bak`;
  let n = 0;
  while (fs.existsSync(backup)) {
    n += 1;
    backup = `${filePath}.bak.${n}`;
  }
  fs.copyFileSync(filePath, backup);
  try {
    fs.chmodSync(backup, FILE_MODE);
  } catch {
    /* best-effort */
  }
  // Cap only when the new backup itself is managed content.
  try {
    if (isManagedAgentBody(fs.readFileSync(backup, "utf8"))) {
      pruneManagedAgentBackups(filePath, MAX_MANAGED_AGENT_BACKUPS);
    }
  } catch {
    /* best-effort prune */
  }
  return backup;
}

/**
 * Copy / refresh shipped Codex agent templates into the user's Codex agents dir.
 *
 * @returns {{ ok: boolean, destDir: string, companion: string, installed: string[], updated: string[], skipped: string[], skippedUser: string[], backedUp: string[], errors: string[] }}
 */
export function installCodexAgents({
  templatesDir = DEFAULT_TEMPLATES_DIR,
  destDir = null,
  env = process.env,
  force = false,
  updateManaged = true,
  pluginRoot = null,
  backup = true,
  cwd = null,
  scope = null,
} = {}) {
  const root =
    (pluginRoot && String(pluginRoot).trim()) ||
    (env.CLAUDE_PLUGIN_ROOT || env.PLUGIN_ROOT || "").trim() ||
    DEFAULT_PLUGIN_ROOT;
  const companion = resolveCompanionPath(root);
  const agentRun = resolveAgentRunPath(root);
  const workCwd = cwd || process.cwd();
  // Project-scope agent discovery (.codex/agents/) per Codex docs July 2026:
  // https://developers.openai.com/codex/subagents
  // (personal ~/.codex/agents, project .codex/agents).
  const dest =
    destDir ||
    resolveCodexAgentsDestDir({ cwd: workCwd, env, scope });
  const installed = [];
  const updated = [];
  const skipped = [];
  const skippedUser = [];
  const backedUp = [];
  const errors = [];
  const templates = listTemplateAgents(templatesDir);

  if (!templates.length) {
    return {
      ok: false,
      destDir: dest,
      companion,
      agentRun,
      installed,
      updated,
      skipped,
      skippedUser,
      backedUp,
      errors: [`no .toml templates in ${templatesDir}`],
    };
  }
  if (!fs.existsSync(companion)) {
    return {
      ok: false,
      destDir: dest,
      companion,
      agentRun,
      installed,
      updated,
      skipped,
      skippedUser,
      backedUp,
      errors: [`companion not found at ${companion}`],
    };
  }
  if (!fs.existsSync(agentRun)) {
    return {
      ok: false,
      destDir: dest,
      companion,
      agentRun,
      installed,
      updated,
      skipped,
      skippedUser,
      backedUp,
      errors: [`agent runner not found at ${agentRun}`],
    };
  }

  try {
    mkdirPrivate(dest);
  } catch (err) {
    return {
      ok: false,
      destDir: dest,
      companion,
      agentRun,
      installed,
      updated,
      skipped,
      skippedUser,
      backedUp,
      errors: [`cannot create ${dest}: ${err.message}`],
    };
  }

  for (const t of templates) {
    const target = path.join(dest, `${t.name}.toml`);
    if (path.basename(target) !== `${t.name}.toml`) {
      errors.push(`${t.name}: invalid template name`);
      continue;
    }
    try {
      const sourceBody = fs.readFileSync(t.source, "utf8");
      const body = materializeAgentBody(sourceBody, agentRun, companion);

      if (!fs.existsSync(target)) {
        writePrivate(target, body);
        installed.push(t.name);
        continue;
      }

      const existing = fs.readFileSync(target, "utf8");
      if (existing === body) {
        skipped.push(t.name);
        continue;
      }

      const managed = isManagedAgentBody(existing);
      const shouldWrite = (managed && updateManaged) || force;
      if (!shouldWrite) {
        if (managed && !updateManaged) {
          skipped.push(t.name);
        } else {
          skippedUser.push(t.name);
        }
        continue;
      }

      if (backup) {
        const bak = backupAgentFile(target);
        if (bak) {
          backedUp.push(path.basename(bak));
        }
      }
      writePrivate(target, body);
      updated.push(t.name);
    } catch (err) {
      errors.push(`${t.name}: ${err.message}`);
    }
  }

  return {
    ok: errors.length === 0,
    destDir: dest,
    companion,
    agentRun,
    installed,
    updated,
    skipped,
    skippedUser,
    backedUp,
    errors,
  };
}

/**
 * Remove managed grok-skills agents only (files with managed-by header).
 * Leaves user-owned TOML alone. Optional backup before delete.
 *
 * @returns {{ ok: boolean, destDir: string, removed: string[], skippedUser: string[], backedUp: string[], errors: string[] }}
 */
export function uninstallCodexAgents({
  destDir = null,
  env = process.env,
  backup = true,
  onlyNames = null,
  cwd = null,
  scope = null,
} = {}) {
  const workCwd = cwd || process.cwd();
  const dest =
    destDir ||
    resolveCodexAgentsDestDir({ cwd: workCwd, env, scope });
  const removed = [];
  const skippedUser = [];
  const backedUp = [];
  const errors = [];

  if (!fs.existsSync(dest)) {
    return { ok: true, destDir: dest, removed, skippedUser, backedUp, errors };
  }

  let names;
  try {
    names = fs
      .readdirSync(dest)
      .filter((n) => n.endsWith(".toml") && n.startsWith(MANAGED_NAME_PREFIX))
      .map((n) => n.replace(/\.toml$/, ""));
  } catch (err) {
    return {
      ok: false,
      destDir: dest,
      removed,
      skippedUser,
      backedUp,
      errors: [`cannot list ${dest}: ${err.message}`],
    };
  }

  if (onlyNames && onlyNames.length) {
    const allow = new Set(onlyNames);
    names = names.filter((n) => allow.has(n));
  }

  for (const name of names) {
    const target = path.join(dest, `${name}.toml`);
    if (path.basename(target) !== `${name}.toml`) {
      errors.push(`${name}: invalid name`);
      continue;
    }
    try {
      if (!fs.existsSync(target)) {
        continue;
      }
      const body = fs.readFileSync(target, "utf8");
      if (!isManagedAgentBody(body)) {
        skippedUser.push(name);
        continue;
      }
      if (backup) {
        const bak = backupAgentFile(target);
        if (bak) {
          backedUp.push(path.basename(bak));
        }
      }
      fs.unlinkSync(target);
      removed.push(name);
    } catch (err) {
      errors.push(`${name}: ${err.message}`);
    }
  }

  return {
    ok: errors.length === 0,
    destDir: dest,
    removed,
    skippedUser,
    backedUp,
    errors,
  };
}

/**
 * Silent, best-effort ensure for SessionStart. Never throws.
 * Honors workspace prefs scope (user|project) when destDir is omitted.
 */
export function ensureCodexAgents(opts = {}) {
  try {
    return installCodexAgents({
      updateManaged: true,
      force: false,
      backup: true,
      ...opts,
    });
  } catch (err) {
    const env = opts.env || process.env;
    const workCwd = opts.cwd || process.cwd();
    return {
      ok: false,
      destDir:
        opts.destDir ||
        resolveCodexAgentsDestDir({ cwd: workCwd, env, scope: opts.scope }),
      companion: "",
      installed: [],
      updated: [],
      skipped: [],
      skippedUser: [],
      backedUp: [],
      errors: [err.message || String(err)],
    };
  }
}
