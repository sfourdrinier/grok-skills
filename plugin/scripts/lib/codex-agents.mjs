// plugin/scripts/lib/codex-agents.mjs
//
// Install Codex custom-agent TOML templates shipped under plugin/codex-agents/
// into ~/.codex/agents/ (or CODEX_HOME/agents). One-command setup path for
// grok-engineer-coder and grok-rescue.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DIR_MODE = 0o700;
const FILE_MODE = 0o600;

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_TEMPLATES_DIR = path.resolve(SCRIPT_DIR, "..", "..", "codex-agents");

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
    .filter((name) => name.endsWith(".toml"))
    .map((name) => ({
      name: name.replace(/\.toml$/, ""),
      source: path.join(templatesDir, name),
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
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
 * Copy all shipped Codex agent templates into the user's Codex agents dir.
 * @returns {{ ok: boolean, destDir: string, installed: string[], skipped: string[], errors: string[] }}
 */
export function installCodexAgents({
  templatesDir = DEFAULT_TEMPLATES_DIR,
  destDir = null,
  env = process.env,
  force = false,
} = {}) {
  const dest = destDir || codexAgentsDir(env);
  const installed = [];
  const skipped = [];
  const errors = [];
  const templates = listTemplateAgents(templatesDir);
  if (!templates.length) {
    return {
      ok: false,
      destDir: dest,
      installed,
      skipped,
      errors: [`no .toml templates in ${templatesDir}`],
    };
  }
  try {
    mkdirPrivate(dest);
  } catch (err) {
    return {
      ok: false,
      destDir: dest,
      installed,
      skipped,
      errors: [`cannot create ${dest}: ${err.message}`],
    };
  }
  for (const t of templates) {
    const target = path.join(dest, `${t.name}.toml`);
    try {
      if (fs.existsSync(target) && !force) {
        skipped.push(t.name);
        continue;
      }
      const body = fs.readFileSync(t.source, "utf8");
      writePrivate(target, body);
      installed.push(t.name);
    } catch (err) {
      errors.push(`${t.name}: ${err.message}`);
    }
  }
  return {
    ok: errors.length === 0,
    destDir: dest,
    installed,
    skipped,
    errors,
  };
}
