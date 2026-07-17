// plugin/scripts/tests/helpers/fake-wrapper.mjs
//
// Canonical fake-wrapper harness for companion tests. Companion tests never
// spawn the real wrapper or the Grok CLI: makeFakeWrapper writes a temp Python
// script that answers per-mode from FAKE_WRAPPER_RESPONSES, and runCompanion
// spawns grok-companion.mjs against it via the documented override pair
// (GROK_AGENT_WRAPPER + GROK_ALLOW_WRAPPER_OVERRIDE=1, lib/wrapper.mjs).
// An UNREGISTERED mode exits 2 - tests use that as the "this wrapper mode must
// not have been spawned" probe.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HELPERS_DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPANION = path.resolve(HELPERS_DIR, "..", "..", "grok-companion.mjs");

const FAKE_WRAPPER_BODY = `import json, os, sys
responses = json.loads(os.environ.get("FAKE_WRAPPER_RESPONSES", "{}"))
mode = sys.argv[1] if len(sys.argv) > 1 else ""
r = responses.get(mode)
if r is None:
    sys.stderr.write("[fake-wrapper] unregistered mode: %r\\n" % mode)
    sys.exit(2)
if r.get("stderr"):
    sys.stderr.write(r["stderr"])
sys.stdout.write(r.get("stdout", "{}"))
sys.exit(int(r.get("exitCode", 0)))
`;

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

export function runCompanion(argv, { env = {}, cwd = process.cwd(), stdin } = {}) {
  const result = spawnSync(process.execPath, [COMPANION, ...argv], {
    cwd,
    encoding: "utf8",
    input: stdin,
    env: { ...process.env, ...env },
  });
  return {
    code: typeof result.status === "number" ? result.status : 1,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
  };
}
