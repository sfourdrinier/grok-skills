// plugin/scripts/lib/skill-run.mjs
//
// Transparent Skill-tool entry: each skills/<name>/run.mjs calls runFromSkillEntry
// with import.meta.url. Plugin root is derived from the on-disk location of that
// file (skills/<name>/run.mjs -> plugin root). No CLAUDE_PLUGIN_ROOT required.
//
// The model only substitutes the host-provided Skill base directory:
//   node "$SKILL_BASE/run.mjs" <companion-mode> [args...]

import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  companionPath,
  isValidPluginRoot,
  pluginRootFromSkillDir,
} from "./resolve-plugin-root.mjs";

/**
 * Plugin root from a skill entry module URL (…/skills/<name>/run.mjs).
 * @param {string} importMetaUrl
 * @returns {string}
 */
export function pluginRootFromSkillEntryUrl(importMetaUrl) {
  const entryFile = fileURLToPath(importMetaUrl);
  return pluginRootFromSkillDir(path.dirname(entryFile));
}

/**
 * Spawn grok-companion with argv. Returns process exit code.
 * @param {string} importMetaUrl - import.meta.url of skills/<name>/run.mjs
 * @param {string[]} argv - forwarded to companion (mode + flags)
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {number}
 */
export function runFromSkillEntry(importMetaUrl, argv, env = process.env) {
  const root = pluginRootFromSkillEntryUrl(importMetaUrl);
  if (!isValidPluginRoot(root)) {
    process.stderr.write(
      `skill-run: invalid plugin root at ${root} (missing scripts/grok-companion.mjs)\n`
    );
    return 127;
  }
  const companion = companionPath(root);
  const childEnv = {
    ...env,
    // Propagate so nested wrapper resolution and child tools see a root.
    CLAUDE_PLUGIN_ROOT: (env.CLAUDE_PLUGIN_ROOT || "").trim() || root,
    PLUGIN_ROOT: (env.PLUGIN_ROOT || "").trim() || root,
  };

  const result = spawnSync(process.execPath, [companion, ...argv], {
    stdio: "inherit",
    env: childEnv,
  });

  if (result.error) {
    process.stderr.write(`skill-run: failed to spawn companion: ${result.error.message}\n`);
    return 127;
  }
  if (result.signal) {
    return 1;
  }
  return typeof result.status === "number" ? result.status : 1;
}
