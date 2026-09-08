// plugin/scripts/lib/jobs-layout.mjs
//
// Workspace state root, jobs dir, and one-time tmp -> CLAUDE_PLUGIN_DATA
// migration. Split from jobs.mjs for the 900-line cap.

import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { FILE_MODE, mkdirPrivate } from "./atomic-file.mjs";
import { resolveWorkspaceRoot } from "./gate-state.mjs";

const PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA";
export const DURABLE_STATE_FALLBACK = path.join(os.homedir(), ".grok-skills", "state");
export const LEGACY_TMP_STATE_FALLBACK = path.join(os.tmpdir(), "grok-companion");
const FALLBACK = DURABLE_STATE_FALLBACK;
const LEGACY_TMP_FALLBACK = LEGACY_TMP_STATE_FALLBACK;

function workspaceStateSegment(cwd) {
  const workspaceRoot = resolveWorkspaceRoot(cwd);
  let canonical = workspaceRoot;
  try {
    canonical = fs.realpathSync.native(workspaceRoot);
  } catch {
    canonical = workspaceRoot;
  }
  const slug =
    (path.basename(workspaceRoot) || "workspace")
      .replace(/[^a-zA-Z0-9._-]+/g, "-")
      .replace(/^-+|-+$/g, "") || "workspace";
  const hash = createHash("sha256").update(canonical).digest("hex").slice(0, 16);
  return `${slug}-${hash}`;
}

function resolvePluginDataDir(env = process.env) {
  const raw = (env[PLUGIN_DATA_ENV] ?? env.PLUGIN_DATA ?? "").trim();
  if (!raw || !path.isAbsolute(raw)) {
    return null;
  }
  return raw;
}

function atomicCopyFile(src, dest) {
  const dir = path.dirname(dest);
  mkdirPrivate(dir);
  const tmp = path.join(
    dir,
    `.${path.basename(dest)}.tmp-${process.pid}-${randomBytes(4).toString("hex")}`
  );
  try {
    fs.copyFileSync(src, tmp);
    try {
      fs.chmodSync(tmp, FILE_MODE);
    } catch {
      /* best-effort */
    }
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

function copyJobBodyTree(srcDir, destDir) {
  mkdirPrivate(destDir);
  const entries = fs.readdirSync(srcDir, { withFileTypes: true });
  for (const entry of entries) {
    const from = path.join(srcDir, entry.name);
    const to = path.join(destDir, entry.name);
    if (entry.isDirectory()) {
      copyJobBodyTree(from, to);
    } else if (entry.isFile()) {
      fs.copyFileSync(from, to);
      try {
        fs.chmodSync(to, FILE_MODE);
      } catch {
        /* best-effort */
      }
    }
  }
}

function maybeMigrateLegacyState(legacyDir, newDir) {
  try {
    const newIndex = path.join(newDir, "jobs-index.json");
    if (fs.existsSync(newIndex)) {
      return;
    }
    if (!fs.existsSync(legacyDir)) {
      return;
    }
    const legacyIndex = path.join(legacyDir, "jobs-index.json");
    const legacyPrefs = path.join(legacyDir, "prefs.json");
    if (!fs.existsSync(legacyIndex) && !fs.existsSync(legacyPrefs)) {
      return;
    }
    mkdirPrivate(newDir);

    const legacyJobs = path.join(legacyDir, "jobs");
    const newJobs = path.join(newDir, "jobs");
    if (fs.existsSync(legacyJobs)) {
      mkdirPrivate(newJobs);
      let entries = [];
      try {
        entries = fs.readdirSync(legacyJobs, { withFileTypes: true });
      } catch (err) {
        process.stderr.write(
          `[grok-jobs] job body migration partial (list): ${err?.message ?? err}\n`
        );
        entries = [];
      }
      for (const entry of entries) {
        try {
          const from = path.join(legacyJobs, entry.name);
          const to = path.join(newJobs, entry.name);
          if (entry.isDirectory()) {
            copyJobBodyTree(from, to);
          } else if (entry.isFile()) {
            fs.copyFileSync(from, to);
            try {
              fs.chmodSync(to, FILE_MODE);
            } catch {
              /* best-effort */
            }
          }
        } catch (err) {
          try {
            process.stderr.write(
              `[grok-jobs] job body migration partial for ${entry.name}: ${err?.message ?? err}\n`
            );
          } catch {
            /* best-effort */
          }
        }
      }
    }

    if (fs.existsSync(legacyIndex)) {
      atomicCopyFile(legacyIndex, newIndex);
    }
    if (fs.existsSync(legacyPrefs)) {
      atomicCopyFile(legacyPrefs, path.join(newDir, "prefs.json"));
    }
    const legacyBak = path.join(legacyDir, "prefs.json.bak");
    if (fs.existsSync(legacyBak)) {
      atomicCopyFile(legacyBak, path.join(newDir, "prefs.json.bak"));
    }
    process.stderr.write(
      `[grok-jobs] migrated workspace state from ${legacyDir} to ${newDir}\n`
    );
  } catch (err) {
    try {
      process.stderr.write(
        `[grok-jobs] state migration skipped: ${err?.message ?? err}\n`
      );
    } catch {
      /* best-effort */
    }
  }
}

export function stateRoot(cwd, env = process.env) {
  const segment = workspaceStateSegment(cwd);
  const legacyTmpDir = path.join(LEGACY_TMP_FALLBACK, segment);
  const durableDir = path.join(FALLBACK, segment);
  const pluginData = resolvePluginDataDir(env);
  if (pluginData) {
    const newDir = path.join(pluginData, "state", segment);
    maybeMigrateLegacyState(legacyTmpDir, newDir);
    maybeMigrateLegacyState(durableDir, newDir);
    return newDir;
  }
  maybeMigrateLegacyState(legacyTmpDir, durableDir);
  return durableDir;
}

export function jobsDir(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), "jobs");
}
