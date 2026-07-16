// plugin/scripts/lib/codex-agents.mjs
//
// Install Codex custom-agent TOML templates shipped under plugin/codex-agents/
// into ~/.codex/agents/ (or CODEX_HOME/agents).
//
// Codex does not yet register plugin-bundled agents (openai/codex#18988), so we
// materialize them into the global agents dir. Prefer ensureCodexAgents() from
// SessionStart so install is zero-step for the user. Templates use
// __GROK_AGENT_RUN_Q__; install rewrites an absolute path to agents/run.mjs.

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DIR_MODE = 0o700;
const FILE_MODE = 0o600;
const MANAGED_BY = "grok-skills";
const AGENT_RUN_PLACEHOLDER = "__GROK_AGENT_RUN_Q__";
/** @deprecated legacy templates */
const COMPANION_PLACEHOLDER = "__GROK_COMPANION_Q__";
const MANAGED_NAME_PREFIX = "grok-";

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
  fs.writeFileSync(filePath, content, { encoding: "utf8", mode: FILE_MODE });
  try {
    fs.chmodSync(filePath, FILE_MODE);
  } catch {
    /* best-effort */
  }
}

/**
 * Backup existing file to path.bak (and path.bak.N if needed). Returns backup path or null.
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
} = {}) {
  const root =
    (pluginRoot && String(pluginRoot).trim()) ||
    (env.CLAUDE_PLUGIN_ROOT || env.PLUGIN_ROOT || "").trim() ||
    DEFAULT_PLUGIN_ROOT;
  const companion = resolveCompanionPath(root);
  const agentRun = resolveAgentRunPath(root);
  const dest = destDir || codexAgentsDir(env);
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
} = {}) {
  const dest = destDir || codexAgentsDir(env);
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
    return {
      ok: false,
      destDir: opts.destDir || codexAgentsDir(opts.env || process.env),
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
