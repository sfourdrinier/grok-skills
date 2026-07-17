// plugin/scripts/lib/gate-state.mjs
//
// Tiny per-workspace state file for the opt-in stop-review gate. Stores a
// single boolean. Kept separate from the wrapper: this is plugin-local UX
// state, never a safety boundary. Location mirrors the jobs registry: under
// CLAUDE_PLUGIN_DATA/state when set to an absolute path, else an OS temp
// fallback, keyed by a slug + hash of the workspace root so distinct repos
// never collide. Best-effort migration copies gate-state.json from the legacy
// tmp root when CLAUDE_PLUGIN_DATA appears (complete only when the new file
// exists; dir-exists alone is not enough).

import { createHash, randomBytes } from "node:crypto";
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
 * Per-workspace state segment: `<basename-slug>-<sha256(canonical)[0:16]>`.
 * Kept identical for legacy tmp and CLAUDE_PLUGIN_DATA layouts so migration
 * and dual-path lookups share one key.
 * @param {string} cwd
 * @returns {string}
 */
function workspaceStateSegment(cwd) {
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
  return `${slug}-${hash}`;
}

/**
 * Absolute CLAUDE_PLUGIN_DATA only. Relative / empty -> null.
 * @param {Record<string, string | undefined>} env
 * @returns {string|null}
 */
function resolvePluginDataDir(env = process.env) {
  const raw = (env[PLUGIN_DATA_ENV] ?? "").trim();
  if (!raw || !path.isAbsolute(raw)) {
    return null;
  }
  return raw;
}

/**
 * Atomic file copy via temp + rename (same filesystem).
 * @param {string} src
 * @param {string} dest
 */
function atomicCopyFile(src, dest) {
  const dir = path.dirname(dest);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = path.join(
    dir,
    `.${path.basename(dest)}.tmp-${process.pid}-${randomBytes(4).toString("hex")}`
  );
  try {
    fs.copyFileSync(src, tmp);
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
 * Best-effort one-time copy of gate-state.json from the legacy tmp root into
 * CLAUDE_PLUGIN_DATA/state. Complete only when the new file exists (dir-exists
 * alone is not enough - retry partials). Legacy left as a frozen snapshot.
 * Never throws; notes on stderr.
 * @param {string} legacyDir
 * @param {string} newDir
 */
function maybeMigrateLegacyGateState(legacyDir, newDir) {
  try {
    const newFile = path.join(newDir, STATE_FILE_NAME);
    if (fs.existsSync(newFile)) {
      return;
    }
    const legacyFile = path.join(legacyDir, STATE_FILE_NAME);
    if (!fs.existsSync(legacyFile)) {
      return;
    }
    fs.mkdirSync(newDir, { recursive: true });
    atomicCopyFile(legacyFile, newFile);
    process.stderr.write(
      `[grok-gate] migrated gate state from ${legacyDir} to ${newDir}\n`
    );
  } catch (err) {
    try {
      process.stderr.write(
        `[grok-gate] state migration skipped: ${err?.message ?? err}\n`
      );
    } catch {
      /* best-effort */
    }
  }
}

/**
 * @param {string} cwd
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function resolveStateDir(cwd, env = process.env) {
  const segment = workspaceStateSegment(cwd);
  const legacyDir = path.join(FALLBACK_STATE_ROOT_DIR, segment);
  const pluginData = resolvePluginDataDir(env);
  if (pluginData) {
    const newDir = path.join(pluginData, "state", segment);
    maybeMigrateLegacyGateState(legacyDir, newDir);
    return newDir;
  }
  return legacyDir;
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
