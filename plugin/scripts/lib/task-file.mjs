// plugin/scripts/lib/task-file.mjs
//
// Shared temp staging for companion task payloads. Single owner of mkdtemp +
// 0600 write + best-effort recursive cleanup so stdin staging and task inject
// stay DRY. Also owns extractTask (read --task / --task-file from argv) so
// companion and direct-mode share one source.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { readAllStdinSync } from "./read-stdin.mjs";

/**
 * Read task text from argv: prefer --task-file path (not "-"), else --task.
 * Missing/unreadable --task-file yields "" (caller decides fail-closed).
 * @param {string[]} args
 * @returns {string}
 */
export function extractTask(args) {
  if (!Array.isArray(args)) return "";
  // Accept BOTH `--flag value` and `--flag=value` (the hardened wrapper's
  // argparse takes both, so the direct path must too, or `code --task=...`
  // fails with a spurious "requires --task" before Grok runs).
  const tf = taskFlagValue(args, "--task-file");
  if (tf && tf !== "-") {
    try {
      return fs.readFileSync(tf, "utf8");
    } catch {
      return "";
    }
  }
  const t = taskFlagValue(args, "--task");
  if (t) return t;
  return "";
}

/** First value for `--flag value` or `--flag=value`; null when absent. */
function taskFlagValue(args, name) {
  const eq = name + "=";
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === name && args[i + 1] !== undefined) return args[i + 1];
    if (typeof a === "string" && a.startsWith(eq)) return a.slice(eq.length);
  }
  return null;
}

/**
 * Stage task text (string or Buffer) under a private temp dir.
 * @param {string|Buffer} taskText
 * @returns {{ taskPath: string, cleanup: () => void }}
 */
export function stageTaskFile(taskText) {
  const stagingDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-task-"));
  const taskPath = path.join(stagingDir, "task");
  fs.writeFileSync(taskPath, taskText, { mode: 0o600 });
  const cleanup = () => {
    try {
      fs.rmSync(stagingDir, { recursive: true, force: true });
    } catch {
      // best-effort
    }
  };
  return { taskPath, cleanup };
}

/**
 * When argv contains `--task-file -`, read all stdin, stage bytes, and replace
 * the `-` with the staged path. Returns null when the sentinel is absent.
 * @param {string[]} args
 * @returns {{ args: string[], cleanup: () => void } | null}
 */
export function stageStdinTaskFile(args) {
  // Accept BOTH the split `--task-file -` and the equals `--task-file=-` stdin
  // sentinels (parity with extractTask/injectTaskFile, which are equals-aware):
  // the wrapper's argparse takes both, so the companion must stage stdin for both
  // or the literal "-" reaches the wrapper and fails as a missing task file.
  const splitIdx = args.indexOf("--task-file");
  let valueIndex = -1; // argv slot to overwrite with the staged path
  let equalsForm = false;
  if (splitIdx >= 0 && args[splitIdx + 1] === "-") {
    valueIndex = splitIdx + 1;
  } else {
    const eqIdx = args.findIndex((a) => a === "--task-file=-");
    if (eqIdx >= 0) {
      valueIndex = eqIdx;
      equalsForm = true;
    }
  }
  if (valueIndex < 0) return null;
  const taskBytes = readAllStdinSync();
  const { taskPath, cleanup } = stageTaskFile(taskBytes);
  const staged = args.slice();
  staged[valueIndex] = equalsForm ? `--task-file=${taskPath}` : taskPath;
  return { args: staged, cleanup };
}

/**
 * Strip any `--task` / `--task-file` pairs and append `--task-file` pointing
 * at a freshly staged file containing taskText.
 * @param {string[]} args
 * @param {string|Buffer} taskText
 * @returns {{ args: string[], cleanup: () => void }}
 */
export function injectTaskFile(args, taskText) {
  const cleaned = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--task" || a === "--task-file") {
      i += 1;
      continue;
    }
    if (a.startsWith("--task=") || a.startsWith("--task-file=")) {
      continue;
    }
    cleaned.push(a);
  }
  const { taskPath, cleanup } = stageTaskFile(taskText);
  cleaned.push("--task-file", taskPath);
  return { args: cleaned, cleanup };
}
