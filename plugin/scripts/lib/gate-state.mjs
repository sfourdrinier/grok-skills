// plugin/scripts/lib/gate-state.mjs
//
// Tiny per-workspace state file for the opt-in stop-review gate. Stores a
// single boolean. Kept separate from the wrapper: this is plugin-local UX
// state, never a safety boundary. Location mirrors the Codex plugin: under
// CLAUDE_PLUGIN_DATA/state when set, else an OS temp fallback, keyed by a
// slug + hash of the workspace root so distinct repos never collide.

import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA";
const FALLBACK_STATE_ROOT_DIR = path.join(os.tmpdir(), "grok-companion");
const STATE_FILE_NAME = "gate-state.json";

function defaultState() {
  return { stopReviewGate: false };
}

/**
 * Resolve the workspace root: the nearest git root at or above cwd, else cwd.
 * @param {string} cwd
 * @returns {string}
 */
export function resolveWorkspaceRoot(cwd) {
  let dir = path.resolve(cwd);
  for (;;) {
    if (fs.existsSync(path.join(dir, ".git"))) {
      return dir;
    }
    const parent = path.dirname(dir);
    if (parent === dir) {
      return path.resolve(cwd);
    }
    dir = parent;
  }
}

/**
 * @param {string} cwd
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function resolveStateDir(cwd, env = process.env) {
  const workspaceRoot = resolveWorkspaceRoot(cwd);
  let canonical = workspaceRoot;
  try {
    canonical = fs.realpathSync.native(workspaceRoot);
  } catch (err) {
    // realpath can fail on odd mounts; fall back to the logical path and note it.
    process.stderr.write(`[grok-gate] could not canonicalize ${workspaceRoot}: ${err.message}\n`);
    canonical = workspaceRoot;
  }
  const slugSource = path.basename(workspaceRoot) || "workspace";
  const slug = slugSource.replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "workspace";
  const hash = createHash("sha256").update(canonical).digest("hex").slice(0, 16);
  const pluginData = (env[PLUGIN_DATA_ENV] ?? "").trim();
  const stateRoot = pluginData ? path.join(pluginData, "state") : FALLBACK_STATE_ROOT_DIR;
  return path.join(stateRoot, `${slug}-${hash}`);
}

function resolveStateFile(cwd, env = process.env) {
  return path.join(resolveStateDir(cwd, env), STATE_FILE_NAME);
}

/**
 * @param {string} cwd
 * @param {Record<string, string | undefined>} env
 * @returns {{ stopReviewGate: boolean }}
 */
export function readGateConfig(cwd, env = process.env) {
  const file = resolveStateFile(cwd, env);
  if (!fs.existsSync(file)) {
    return defaultState();
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    return { stopReviewGate: parsed?.stopReviewGate === true };
  } catch (err) {
    // A corrupt state file must not silently enable or disable the gate in a
    // surprising way; log and fall back to the safe default (gate off).
    process.stderr.write(`[grok-gate] could not read ${file}: ${err.message}; defaulting gate off\n`);
    return defaultState();
  }
}

/**
 * @param {string} cwd
 * @param {boolean} enabled
 * @param {Record<string, string | undefined>} env
 * @returns {{ stopReviewGate: boolean }}
 */
export function writeGateConfig(cwd, enabled, env = process.env) {
  const dir = resolveStateDir(cwd, env);
  fs.mkdirSync(dir, { recursive: true });
  const state = { stopReviewGate: enabled === true };
  fs.writeFileSync(path.join(dir, STATE_FILE_NAME), `${JSON.stringify(state, null, 2)}\n`, { encoding: "utf8" });
  return state;
}
