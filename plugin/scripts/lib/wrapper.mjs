// plugin/scripts/lib/wrapper.mjs
//
// Resolve the on-disk path to the Grok wrapper entrypoint (grok_agent.py).
// The plugin adds NO safety logic: it only locates the hardened wrapper and
// forwards to it. Resolution is fail-closed - an unfound wrapper is an error,
// never a fabricated success.
//
// Ship-ready layout: the wrapper is BUNDLED inside the plugin install tree at
// <plugin-root>/wrapper/scripts/grok_agent.py. Claude Code marketplace installs
// copy only the plugin directory into a cache; nesting the wrapper inside the
// plugin is what makes installs work without GROK_AGENT_WRAPPER.
//
// Path candidates (first existing wins):
//   1. GROK_AGENT_WRAPPER env override (tests / advanced operators)
//   2. ${CLAUDE_PLUGIN_ROOT}/wrapper/scripts/grok_agent.py
//   3. ${PLUGIN_ROOT}/wrapper/scripts/grok_agent.py (Codex plugin hooks)
//   4. Path derived from this script: scripts/lib -> plugin root -> wrapper/...

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WRAPPER_RELATIVE_FROM_PLUGIN_ROOT = path.join(
  "wrapper",
  "scripts",
  "grok_agent.py"
);

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));

/**
 * Return the candidate wrapper paths in priority order, without checking
 * existence. Kept pure so it can be unit-tested with an injected env.
 *
 * @param {Record<string, string | undefined>} env
 * @returns {string[]}
 */
export function candidateWrapperPaths(env = process.env) {
  const candidates = [];
  const seen = new Set();

  const push = (raw) => {
    if (!raw) return;
    const resolved = path.resolve(raw);
    if (seen.has(resolved)) return;
    seen.add(resolved);
    candidates.push(resolved);
  };

  const override = (env.GROK_AGENT_WRAPPER ?? "").trim();
  if (override) {
    push(override);
  }

  const pluginRoot = (env.CLAUDE_PLUGIN_ROOT ?? "").trim();
  if (pluginRoot) {
    push(path.join(pluginRoot, WRAPPER_RELATIVE_FROM_PLUGIN_ROOT));
  }

  // Codex sets PLUGIN_ROOT (and CLAUDE_PLUGIN_ROOT for compatibility). Prefer
  // the Codex-native name when present so installs that only export PLUGIN_ROOT
  // still resolve.
  const codexPluginRoot = (env.PLUGIN_ROOT ?? "").trim();
  if (codexPluginRoot) {
    push(path.join(codexPluginRoot, WRAPPER_RELATIVE_FROM_PLUGIN_ROOT));
  }

  // Fallback derived from this script's own location:
  // scripts/lib -> scripts -> plugin root.
  const derivedPluginRoot = path.resolve(SCRIPT_DIR, "..", "..");
  push(path.join(derivedPluginRoot, WRAPPER_RELATIVE_FROM_PLUGIN_ROOT));

  return candidates;
}

/**
 * Resolve the first existing wrapper path. Returns null when none exist so the
 * caller can fail closed with an actionable message.
 *
 * @param {Record<string, string | undefined>} env
 * @returns {string | null}
 */
export function resolveWrapperPath(env = process.env) {
  for (const candidate of candidateWrapperPaths(env)) {
    try {
      if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
        return candidate;
      }
    } catch (err) {
      // A candidate we cannot stat is simply not usable; log and keep looking
      // rather than crash. The loop still fails closed if none resolve.
      process.stderr.write(`[grok-companion] could not stat wrapper candidate ${candidate}: ${err.message}\n`);
    }
  }
  return null;
}

/**
 * The actionable message printed to stderr when the wrapper cannot be found.
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function wrapperNotFoundMessage(env = process.env) {
  const tried = candidateWrapperPaths(env).join("\n  ");
  return [
    "[grok-companion] Could not locate the Grok wrapper (grok_agent.py).",
    "Tried these paths in order:",
    `  ${tried}`,
    "",
    "The wrapper must live at <plugin-root>/wrapper/scripts/grok_agent.py",
    "(bundled with this plugin). Reinstall the plugin, run /grok:setup, or set",
    "GROK_AGENT_WRAPPER to the absolute path of grok_agent.py."
  ].join("\n");
}
