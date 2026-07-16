// plugin/scripts/lib/resolve-plugin-root.mjs
//
// Resolve the grok-skills plugin install root for skills, agents, and tools.
//
// Claude Code injects CLAUDE_PLUGIN_ROOT for hooks and some command expansions,
// but NOT for Bash tool shells that run after a Skill-tool load (markdown only).
// Codex may set PLUGIN_ROOT for plugin hooks depending on host version.
//
// Layout-true fallback: when the host provides the absolute skill directory
// (Skill tool "Base directory for this skill" = .../skills/<name>), plugin root
// is always the parent of skills/. That follows the shipped tree shape; it is
// not inventing a versioned cache path.
//
// OpenAI's codex-for-Claude plugin avoids the Skill-tool gap by using commands/
// with harness-expanded ${CLAUDE_PLUGIN_ROOT} and disable-model-invocation on
// those commands. We support Skill-tool invocation, so skills must resolve root
// without relying solely on env.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const COMPANION_REL = path.join("scripts", "grok-companion.mjs");

const THIS_DIR = path.dirname(fileURLToPath(import.meta.url));
/** plugin/scripts/lib -> plugin root */
export const BUNDLED_PLUGIN_ROOT = path.resolve(THIS_DIR, "..", "..");

/**
 * @param {string} pluginRoot
 * @returns {string}
 */
export function companionPath(pluginRoot) {
  return path.join(path.resolve(pluginRoot), COMPANION_REL);
}

/**
 * True when root looks like a grok-skills install (companion present).
 * @param {string} pluginRoot
 */
export function isValidPluginRoot(pluginRoot) {
  try {
    const companion = companionPath(pluginRoot);
    return fs.existsSync(companion) && fs.statSync(companion).isFile();
  } catch {
    return false;
  }
}

/**
 * From .../skills/<skillName>, .../skills/<skillName>/, or .../SKILL.md
 * return the plugin root (directory that contains skills/ and scripts/).
 * @param {string} skillDir
 * @returns {string}
 */
export function pluginRootFromSkillDir(skillDir) {
  let resolved = path.resolve(skillDir);
  if (path.basename(resolved) === "SKILL.md") {
    resolved = path.dirname(resolved);
  }
  // .../skills/<name>
  if (path.basename(path.dirname(resolved)) === "skills") {
    return path.dirname(path.dirname(resolved));
  }
  // .../skills
  if (path.basename(resolved) === "skills") {
    return path.dirname(resolved);
  }
  // Last resort: two levels up (skills/<name> layout)
  return path.resolve(resolved, "..", "..");
}

/**
 * Resolve plugin root from env and optional skill directory.
 *
 * Priority:
 *  1. CLAUDE_PLUGIN_ROOT
 *  2. PLUGIN_ROOT
 *  3. skillDir argument / SKILL_DIR / GROK_SKILL_DIR
 *
 * @param {object} [opts]
 * @param {NodeJS.ProcessEnv} [opts.env]
 * @param {string|null} [opts.skillDir]
 * @returns {{ root: string|null, source: string|null, companion: string|null, error: string|null }}
 */
export function resolvePluginRoot({ env = process.env, skillDir = null } = {}) {
  const candidates = [];

  const fromClaude = (env.CLAUDE_PLUGIN_ROOT ?? "").trim();
  if (fromClaude) {
    candidates.push({ root: path.resolve(fromClaude), source: "CLAUDE_PLUGIN_ROOT" });
  }
  const fromPlugin = (env.PLUGIN_ROOT ?? "").trim();
  if (fromPlugin) {
    candidates.push({ root: path.resolve(fromPlugin), source: "PLUGIN_ROOT" });
  }

  const skill =
    (skillDir && String(skillDir).trim()) ||
    (env.SKILL_DIR ?? "").trim() ||
    (env.GROK_SKILL_DIR ?? "").trim() ||
    null;
  if (skill) {
    candidates.push({
      root: pluginRootFromSkillDir(skill),
      source: "skill-dir",
    });
  }

  for (const c of candidates) {
    if (isValidPluginRoot(c.root)) {
      return {
        root: c.root,
        source: c.source,
        companion: companionPath(c.root),
        error: null,
      };
    }
  }

  if (candidates.length) {
    const tried = candidates.map((c) => `${c.source}=${c.root}`).join("; ");
    return {
      root: null,
      source: null,
      companion: null,
      error: `plugin root candidates invalid (missing scripts/grok-companion.mjs): ${tried}`,
    };
  }

  return {
    root: null,
    source: null,
    companion: null,
    error:
      "plugin root not set: set CLAUDE_PLUGIN_ROOT or PLUGIN_ROOT, or set SKILL_DIR to the Skill tool base directory (.../skills/<name>)",
  };
}

/**
 * CLI entry: print absolute plugin root or companion path.
 *   node resolve-plugin-root.mjs [--skill-dir DIR] [--companion] [--json]
 */
export function main(argv = process.argv.slice(2), env = process.env) {
  let skillDir = null;
  let printCompanion = false;
  let asJson = false;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--skill-dir" && argv[i + 1]) {
      skillDir = argv[++i];
    } else if (a === "--companion") {
      printCompanion = true;
    } else if (a === "--json") {
      asJson = true;
    } else if (a === "--help" || a === "-h") {
      process.stdout.write(
        "Usage: resolve-plugin-root.mjs [--skill-dir DIR] [--companion] [--json]\n"
      );
      return 0;
    }
  }
  const result = resolvePluginRoot({ env, skillDir });
  if (asJson) {
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return result.root ? 0 : 1;
  }
  if (!result.root) {
    process.stderr.write(`${result.error}\n`);
    return 1;
  }
  process.stdout.write(`${printCompanion ? result.companion : result.root}\n`);
  return 0;
}

const invokedAsMain =
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;

if (invokedAsMain) {
  process.exit(main());
}
