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
  const tf = args.indexOf("--task-file");
  if (tf >= 0 && args[tf + 1] && args[tf + 1] !== "-") {
    try {
      return fs.readFileSync(args[tf + 1], "utf8");
    } catch {
      return "";
    }
  }
  const t = args.indexOf("--task");
  if (t >= 0 && args[t + 1]) {
    return args[t + 1];
  }
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
 * When argv contains `--task-file -`, read all stdin, stage bytes, and replace
 * the `-` with the staged path. Returns null when the sentinel is absent.
 * @param {string[]} args
 * @returns {{ args: string[], cleanup: () => void } | null}
 */
export function stageStdinTaskFile(args) {
  const flagIndex = args.indexOf("--task-file");
  if (flagIndex < 0 || args[flagIndex + 1] !== "-") {
    return null;
  }
  const taskBytes = readAllStdinSync();
  const { taskPath, cleanup } = stageTaskFile(taskBytes);
  const staged = args.slice();
  staged[flagIndex + 1] = taskPath;
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
    if (args[i] === "--task" || args[i] === "--task-file") {
      i += 1;
      continue;
    }
    cleaned.push(args[i]);
  }
  const { taskPath, cleanup } = stageTaskFile(taskText);
  cleaned.push("--task-file", taskPath);
  return { args: cleaned, cleanup };
}
