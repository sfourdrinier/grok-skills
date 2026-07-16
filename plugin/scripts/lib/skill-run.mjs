// plugin/scripts/lib/skill-run.mjs
//
// Self-locating entry for skills and agents:
//   skills/<name>/run.mjs  -> plugin root = ../..
//   agents/run.mjs         -> plugin root = ..
// No CLAUDE_PLUGIN_ROOT required once the absolute path to run.mjs is known.

import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  companionPath,
  isValidPluginRoot,
  pluginRootFromSkillDir,
} from "./resolve-plugin-root.mjs";

/**
 * Plugin root from a skill entry URL (…/skills/<name>/run.mjs).
 * @param {string} importMetaUrl
 * @returns {string}
 */
export function pluginRootFromSkillEntryUrl(importMetaUrl) {
  const entryFile = fileURLToPath(importMetaUrl);
  return pluginRootFromSkillDir(path.dirname(entryFile));
}

/**
 * Plugin root from any installed entry module:
 * - …/skills/<name>/run.mjs
 * - …/agents/run.mjs
 * @param {string} importMetaUrl
 * @returns {string}
 */
export function pluginRootFromPluginEntryUrl(importMetaUrl) {
  const entryFile = fileURLToPath(importMetaUrl);
  const entryDir = path.dirname(entryFile);
  // agents/run.mjs lives directly under plugin/agents/
  if (path.basename(entryDir) === "agents") {
    return path.resolve(entryDir, "..");
  }
  // skills/<name>/run.mjs
  if (path.basename(path.dirname(entryDir)) === "skills") {
    return path.resolve(entryDir, "..", "..");
  }
  return pluginRootFromSkillDir(entryDir);
}

/**
 * Spawn grok-companion with argv. Returns process exit code.
 * @param {string} importMetaUrl - import.meta.url of skills/<name>/run.mjs or agents/run.mjs
 * @param {string[]} argv - forwarded to companion (mode + flags)
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {number}
 */
export function runFromPluginEntry(importMetaUrl, argv, env = process.env) {
  const root = pluginRootFromPluginEntryUrl(importMetaUrl);
  if (!isValidPluginRoot(root)) {
    process.stderr.write(
      `skill-run: invalid plugin root at ${root} (missing scripts/grok-companion.mjs)\n`
    );
    return 127;
  }
  const companion = companionPath(root);
  // Always force entry-derived root into the child. Stale CLAUDE_PLUGIN_ROOT from
  // hooks/session after a plugin upgrade would otherwise make the companion load
  // the *old* wrapper while run.mjs is from the *new* install.
  const childEnv = {
    ...env,
    CLAUDE_PLUGIN_ROOT: root,
    PLUGIN_ROOT: root,
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

/** @deprecated use runFromPluginEntry */
export function runFromSkillEntry(importMetaUrl, argv, env = process.env) {
  return runFromPluginEntry(importMetaUrl, argv, env);
}
