// plugin/scripts/tests/helpers/fake-wrapper.mjs
//
// Canonical fake-wrapper harness for companion tests. Companion tests never
// spawn the real wrapper or the Grok CLI: makeFakeWrapper writes a temp Python
// script that answers per-mode from FAKE_WRAPPER_RESPONSES, and runCompanion
// spawns grok-companion.mjs against it via the documented override pair
// (GROK_AGENT_WRAPPER + GROK_ALLOW_WRAPPER_OVERRIDE=1, lib/wrapper.mjs).
// An UNREGISTERED mode exits 2 - tests use that as the "this wrapper mode must
// not have been spawned" probe.
// When FAKE_WRAPPER_CALLS points at a file, each invocation appends the mode
// plus newline before responding (real spawn-order assertions).
//
// Isolation: runCompanion / companionIsolation default to a fresh temp cwd,
// XDG_STATE_HOME, TMPDIR, and CLAUDE_PLUGIN_DATA so concurrent suites never
// race on the real XDG state root or a shared workspace job registry. Callers
// that pass their own values keep them (hasOwnProperty on the env object).

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HELPERS_DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPANION = path.resolve(HELPERS_DIR, "..", "..", "grok-companion.mjs");

const FAKE_WRAPPER_BODY = `import json, os, sys
mode = sys.argv[1] if len(sys.argv) > 1 else ""
calls_path = os.environ.get("FAKE_WRAPPER_CALLS", "").strip()
# Count prior calls of this mode BEFORE appending (sequence index).
prior = 0
if calls_path and os.path.exists(calls_path):
    with open(calls_path, "r", encoding="utf-8") as cf:
        prior = sum(1 for line in cf if line.strip() == mode)
if calls_path:
    with open(calls_path, "a", encoding="utf-8") as cf:
        cf.write(mode + "\\n")
responses = json.loads(os.environ.get("FAKE_WRAPPER_RESPONSES", "{}"))
r = responses.get(mode)
if r is None:
    sys.stderr.write("[fake-wrapper] unregistered mode: %r\\n" % mode)
    sys.exit(2)
# Sequence support: handoff responses may be a list (apply-time revalidation).
if isinstance(r, list):
    if not r:
        sys.stderr.write("[fake-wrapper] empty sequence for mode: %r\\n" % mode)
        sys.exit(2)
    idx = prior if prior < len(r) else len(r) - 1
    r = r[idx]
if r.get("stderr"):
    sys.stderr.write(r["stderr"])
# Optional mutate: write a file before responding (apply-time tree race tests).
mutate = r.get("mutate")
if isinstance(mutate, dict) and mutate.get("path"):
    with open(mutate["path"], "w", encoding="utf-8") as mf:
        mf.write(mutate.get("content", ""))
# echoTask: read --task-file and echo content + argv (task-passing tests).
if r.get("echoTask"):
    argv = sys.argv[1:]
    task_echo = None
    if "--task-file" in argv:
        index = argv.index("--task-file")
        if index + 1 < len(argv):
            with open(argv[index + 1], "r", encoding="utf-8") as handle:
                task_echo = handle.read()
    sys.stdout.write(json.dumps({
        "schemaVersion": 1,
        "mode": mode,
        "status": "success",
        "taskEcho": task_echo,
        "argv": argv,
    }))
    sys.exit(int(r.get("exitCode", 0)))
stdout = r.get("stdout", "{}")
# Optional template so tests can assert which --run-id was forwarded.
if "{{RUN_ID}}" in stdout:
    rid = ""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--run-id" and i + 1 < len(args):
            rid = args[i + 1]
        elif isinstance(a, str) and a.startswith("--run-id="):
            rid = a[len("--run-id="):]
    stdout = stdout.replace("{{RUN_ID}}", rid)
sys.stdout.write(stdout)
sys.exit(int(r.get("exitCode", 0)))
`;

/**
 * Read modes appended by the fake wrapper when FAKE_WRAPPER_CALLS is set.
 * @param {string} callsPath
 * @returns {string[]}
 */
export function readCalls(callsPath) {
  if (!callsPath || !fs.existsSync(callsPath)) {
    return [];
  }
  return fs
    .readFileSync(callsPath, "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

export function makeFakeWrapper(responses) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-fake-wrapper-"));
  fs.chmodSync(dir, 0o700);
  const wrapperPath = path.join(dir, "grok_agent.py");
  fs.writeFileSync(wrapperPath, FAKE_WRAPPER_BODY, { mode: 0o600 });
  return {
    wrapperPath,
    env: {
      GROK_AGENT_WRAPPER: wrapperPath,
      GROK_ALLOW_WRAPPER_OVERRIDE: "1",
      FAKE_WRAPPER_RESPONSES: JSON.stringify(responses ?? {}),
    },
    cleanup: () => {
      try {
        fs.rmSync(dir, { recursive: true, force: true });
      } catch {
        // best-effort temp cleanup
      }
    },
  };
}

function hasOwn(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

/**
 * Build an isolated cwd + env for any companion (or companion-adjacent) spawn.
 * Defaults are applied only when the caller did not pass the corresponding
 * option / env key, so explicit fixtures keep full control.
 *
 * @param {{ env?: Record<string, string | undefined>, cwd?: string }} [options]
 * @returns {{ cwd: string, env: NodeJS.ProcessEnv, cleanup: () => void }}
 */
export function companionIsolation({ env: callerEnv = {}, cwd: callerCwd } = {}) {
  const roots = [];
  const mk = (prefix) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
    roots.push(dir);
    return dir;
  };

  const cwd = callerCwd ?? mk("grok-companion-cwd-");
  const isolation = {};

  if (!hasOwn(callerEnv, "XDG_STATE_HOME")) {
    isolation.XDG_STATE_HOME = mk("grok-companion-xdg-");
  }
  if (!hasOwn(callerEnv, "CLAUDE_PLUGIN_DATA") && !hasOwn(callerEnv, "PLUGIN_DATA")) {
    // Always absolute so jobs.mjs resolvePluginDataDir accepts it.
    isolation.CLAUDE_PLUGIN_DATA = path.join(cwd, ".grok-plugin-data");
  }
  if (!hasOwn(callerEnv, "TMPDIR")) {
    const tmp = mk("grok-companion-tmp-");
    isolation.TMPDIR = tmp;
    // Mirror common temp env vars so platform-specific os.tmpdir() still lands
    // inside the private tree (Node honors TMPDIR; some tools also read TMP/TEMP).
    if (!hasOwn(callerEnv, "TMP")) isolation.TMP = tmp;
    if (!hasOwn(callerEnv, "TEMP")) isolation.TEMP = tmp;
  }

  const env = { ...process.env, ...isolation, ...callerEnv };
  return {
    cwd,
    env,
    cleanup: () => {
      for (const dir of roots) {
        try {
          fs.rmSync(dir, { recursive: true, force: true });
        } catch {
          // best-effort temp cleanup
        }
      }
    },
  };
}

/**
 * Spawn grok-companion.mjs under isolated cwd/XDG/TMPDIR/plugin-data by default.
 * Pass cwd or the matching env keys to opt out of individual defaults.
 *
 * @param {string[]} argv
 * @param {{ env?: Record<string, string | undefined>, cwd?: string, stdin?: string }} [options]
 */
export function runCompanion(argv, { env = {}, cwd, stdin } = {}) {
  const iso = companionIsolation({ env, cwd });
  try {
    const result = spawnSync(process.execPath, [COMPANION, ...argv], {
      cwd: iso.cwd,
      encoding: "utf8",
      input: stdin,
      env: iso.env,
    });
    return {
      code: typeof result.status === "number" ? result.status : 1,
      stdout: result.stdout || "",
      stderr: result.stderr || "",
    };
  } finally {
    // Only remove dirs we created (caller-owned cwd/env roots are left alone).
    iso.cleanup();
  }
}
