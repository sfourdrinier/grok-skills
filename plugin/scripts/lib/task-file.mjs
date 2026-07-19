// plugin/scripts/lib/task-file.mjs
//
// Shared temp staging for companion task payloads. Single owner of mkdtemp +
// 0600 write + best-effort recursive cleanup so stdin staging and task inject
// stay DRY. Also owns extractTask (read --task / --task-file from argv) so
// companion and direct-mode share one source. Flag values come from the single
// last-wins parser in companion-args (argparse parity).

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { dropValueFlags, flagOccurrences, flagValue } from "./companion-args.mjs";
import { readAllStdinSync } from "./read-stdin.mjs";

/**
 * Read task text from argv: prefer --task-file path (not "-"), else --task.
 * Missing/unreadable --task-file yields "" (caller decides fail-closed).
 * Duplicate --task / --task-file: last-wins (argparse parity). Cross-flag
 * policy is preserved: any real --task-file outranks --task.
 * @param {string[]} args
 * @returns {string}
 */
export function extractTask(args) {
  if (!Array.isArray(args)) return "";
  // Accept BOTH `--flag value` and `--flag=value` (the hardened wrapper's
  // argparse takes both, so the direct path must too, or `code --task=...`
  // fails with a spurious "requires --task" before Grok runs).
  const tf = flagValue(args, "--task-file");
  if (tf && tf !== "-") {
    try {
      return fs.readFileSync(tf, "utf8");
    } catch {
      return "";
    }
  }
  const t = flagValue(args, "--task");
  if (t) return t;
  return "";
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
 * When argv's LAST --task-file value is the stdin sentinel `-` (split or equals),
 * read all stdin, stage bytes, and replace that last sentinel slot with the
 * staged path. Returns null when the last --task-file is not the sentinel
 * (or when no --task-file is present). Last-wins parity with extractTask.
 * @param {string[]} args
 * @returns {{ args: string[], cleanup: () => void } | null}
 */
export function stageStdinTaskFile(args) {
  // Accept BOTH the split `--task-file -` and the equals `--task-file=-` stdin
  // sentinels (parity with extractTask/injectTaskFile, which are equals-aware):
  // the wrapper's argparse takes both, so the companion must stage stdin for both
  // or the literal "-" reaches the wrapper and fails as a missing task file.
  // Last *valid* occurrence wins via the argv SSOT (a following flag is never a value).
  const occ = flagOccurrences(args, "--task-file");
  let last = null;
  for (const o of occ) {
    if (o.value !== null) last = o;
  }
  if (!last || last.value !== "-") return null;
  const taskBytes = readAllStdinSync();
  const { taskPath, cleanup } = stageTaskFile(taskBytes);
  const staged = args.slice();
  if (last.form === "equals") {
    staged[last.index] = `--task-file=${taskPath}`;
  } else {
    staged[last.valueIndex] = taskPath;
  }
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
  // Shared strip: never consume a following flag as --task / --task-file value.
  const cleaned = dropValueFlags(Array.isArray(args) ? args : [], ["--task", "--task-file"]);
  const { taskPath, cleanup } = stageTaskFile(taskText);
  cleaned.push("--task-file", taskPath);
  return { args: cleaned, cleanup };
}
