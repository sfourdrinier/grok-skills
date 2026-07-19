// plugin/scripts/tests/direct-parity.test.mjs
//
// Task 1.6: direct-mode job-surface parity + honest handoff/status refusal.
// Fake-wrapper only - unregistered mode exit 2 proves no wrapper spawn.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  DIRECT_NO_HANDOFF_MSG,
  DIRECT_RUN_ID_RE,
  isDirectHandoffRequest,
  rawRunIdFlag,
  resolveDirectTimeoutSeconds,
  runDirectGrok,
} from "../lib/direct-grok.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
import {
  createJob,
  setRunMode,
  storeJobStdout,
  updateJob,
} from "../lib/jobs.mjs";
import { makeFakeWrapper, runCompanion } from "./helpers/fake-wrapper.mjs";

const DIRECT_ID = "direct-1234567890";

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-parity-"));
}

test("[4] runDirectGrok redacts secrets in the direct-mode envelope", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-redact-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    const secret = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nprintf '%s\\n' '{"result":"here is a token ${secret} end"}'\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    assert.doesNotMatch(
      res.envelopeText,
      new RegExp(secret),
      `secret must be redacted from the direct envelope: ${res.envelopeText}`
    );
    assert.match(res.envelopeText, /redacted/i, "redaction marker expected");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("[4] direct fallback withholds error.message when redaction cannot run", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-failclosed-"));
  try {
    const secret = "sk-" + "SECRETSECRETSECRETSECRET0123456789";
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Emit the secret on STDERR and exit nonzero -> envelope.error.message = secret.
    fs.writeFileSync(fakeGrok, `#!/bin/sh\nprintf '%s\\n' 'token ${secret} here' 1>&2\nexit 3\n`);
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "/nonexistent-python-interpreter", // redaction cannot run -> fail closed
    });
    assert.doesNotMatch(
      res.envelopeText,
      new RegExp(secret),
      `secret must be withheld from the fail-closed envelope: ${res.envelopeText}`
    );
    assert.match(res.envelopeText, /withheld/i);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok redacts stderr before relaying it to the terminal", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-stderr-"));
  const secret = "sk-" + "STDERRLEAK0123456789ABCDEFGHIJKL";
  const captured = [];
  const orig = process.stderr.write.bind(process.stderr);
  process.stderr.write = (s, enc, cb) => {
    captured.push(String(s));
    if (typeof enc === "function") enc();
    else if (typeof cb === "function") cb();
    return true;
  };
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Echo the secret to STDERR and exit nonzero.
    fs.writeFileSync(fakeGrok, `#!/bin/sh\nprintf '%s\\n' 'boom ${secret} boom' 1>&2\nexit 3\n`);
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
  } finally {
    process.stderr.write = orig;
    fs.rmSync(dir, { recursive: true, force: true });
  }
  const all = captured.join("");
  assert.doesNotMatch(all, new RegExp(secret), `secret must not reach the terminal: ${all}`);
});

// D4(a): direct mode must load operator ~/.grok auth values into the exact-value
// denylist (via production AUTH_FILE_NAMES + registration helpers) so an opaque
// token that matches NO pattern scanner is still stripped from envelope + stderr.
// Pattern-only redaction is not enough for runMode=direct (uses operator home).
test("runDirectGrok D4(a) redacts opaque operator-auth values from envelope and stderr", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-d4a-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-d4a-home-"));
  // Same opaque shape as the Python D4(a) fixture: 40 alnum chars, no secret prefix.
  const opaque = "a1b2c3d4e5f6g7h8i9j0" + "k1l2m3n4o5p6q7r8s9t0";
  const captured = [];
  const orig = process.stderr.write.bind(process.stderr);
  process.stderr.write = (s, enc, cb) => {
    captured.push(String(s));
    if (typeof enc === "function") enc();
    else if (typeof cb === "function") cb();
    return true;
  };
  try {
    fs.mkdirSync(path.join(home, ".grok"), { recursive: true });
    // String leaf >=16 chars under AUTH_FILE_NAMES is the D4(a) denylist input.
    fs.writeFileSync(
      path.join(home, ".grok", "auth.json"),
      JSON.stringify({ apiKey: opaque }),
      { mode: 0o600 }
    );
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Echo the opaque token on BOTH stdout (envelope) and stderr (terminal relay).
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nprintf '%s\\n' 'leak ${opaque} to-stderr' 1>&2\nprintf '%s\\n' '{"result":"token ${opaque} echoed"}'\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x"],
      cwd: dir,
      env: {
        ...process.env,
        HOME: home,
        GROK_AGENT_BINARY: fakeGrok,
      },
      scriptsDir,
      python: "python3",
    });
    assert.equal(res.code, 0, `expected success envelope: ${res.envelopeText}`);
    assert.doesNotMatch(
      res.envelopeText,
      new RegExp(opaque),
      `opaque auth value must be absent from envelope: ${res.envelopeText}`
    );
    assert.match(
      res.envelopeText,
      /redacted-injected-value/,
      "D4(a) injected-value placeholder expected in envelope"
    );
    const allErr = captured.join("");
    assert.doesNotMatch(
      allErr,
      new RegExp(opaque),
      `opaque auth value must be absent from stderr relay: ${allErr}`
    );
  } finally {
    process.stderr.write = orig;
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});

// Unreadable/malformed operator auth must not disable pattern redaction: empty
// denylist is fail-safe, pattern scan + assert still apply (D4(a) never instead-of).
test("runDirectGrok still pattern-redacts when operator auth is unreadable", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-d4a-bad-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-d4a-bad-home-"));
  const secret = "sk-" + "BADAUTHSTILLREDACT0123456789ABCD";
  try {
    fs.mkdirSync(path.join(home, ".grok"), { recursive: true });
    // Unreadable/malformed auth.json: register fails safe (empty denylist).
    fs.writeFileSync(path.join(home, ".grok", "auth.json"), "{not-json", { mode: 0o600 });
    const fakeGrok = path.join(dir, "fake-grok.sh");
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nprintf '%s\\n' '{"result":"here is a token ${secret} end"}'\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x"],
      cwd: dir,
      env: {
        ...process.env,
        HOME: home,
        GROK_AGENT_BINARY: fakeGrok,
      },
      scriptsDir,
      python: "python3",
    });
    assert.equal(res.code, 0, `expected success envelope: ${res.envelopeText}`);
    assert.doesNotMatch(
      res.envelopeText,
      new RegExp(secret),
      `pattern secret must still be redacted with bad auth: ${res.envelopeText}`
    );
    assert.match(res.envelopeText, /redacted/i, "pattern redaction marker expected");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});

// DRY guard: Node must not own a copied auth filename list; REDACT_SCRIPT loads
// AUTH_FILE_NAMES + register/clear helpers from the Python redaction/authhome SSOT.
test("direct REDACT_SCRIPT registers D4(a) denylist via production AUTH_FILE_NAMES helpers", () => {
  const src = fs.readFileSync(
    path.resolve(SCRIPT_DIR, "..", "lib", "direct-grok.mjs"),
    "utf8"
  );
  assert.match(src, /AUTH_FILE_NAMES/, "REDACT_SCRIPT must import AUTH_FILE_NAMES SSOT");
  assert.match(src, /source_grok_dir/, "REDACT_SCRIPT must resolve operator ~/.grok via source_grok_dir");
  assert.match(
    src,
    /register_injected_secrets_from_home/,
    "REDACT_SCRIPT must register via production helper"
  );
  assert.match(src, /redact_injected_secrets/, "REDACT_SCRIPT must apply exact-value denylist redaction");
  assert.match(
    src,
    /clear_injected_secret_denylist/,
    "REDACT_SCRIPT must clear the denylist in finally"
  );
  // No Node-side auth filename list (production AUTH_FILE_NAMES is the only source).
  assert.doesNotMatch(
    src,
    /authFileNames\s*=\s*\[/,
    "Node must not own a copied auth filename list"
  );
});

test("runDirectGrok verify uses --worktree as cwd, not --target", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-wt-"));
  try {
    const worktree = path.join(dir, "retained-wt");
    const target = path.join(dir, "other-target");
    fs.mkdirSync(worktree);
    fs.mkdirSync(target);
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Echo the --cwd the executor passed to the CLI.
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nc=""\nwhile [ $# -gt 0 ]; do\n  if [ "$1" = "--cwd" ]; then c="$2"; fi\n  shift\ndone\nprintf '{"result":"cwd=%s"}\\n' "$c"\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "verify",
      args: ["--target", target, "--worktree", worktree, "--task", "check"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    // The CLI must be pointed at the worktree (path.resolve, as the executor
    // does), not --target.
    const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    assert.match(res.envelopeText, new RegExp(`cwd=${esc(path.resolve(worktree))}`), res.envelopeText);
    assert.doesNotMatch(res.envelopeText, new RegExp(`cwd=${esc(path.resolve(target))}"`));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok appends web tools to the allowlist only when --web", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-webtools-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Echo the --tools allowlist the CLI received.
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nt=""\nwhile [ $# -gt 0 ]; do\n  if [ "$1" = "--tools" ]; then t="$2"; fi\n  shift\ndone\nprintf '{"result":"tools=%s"}\\n' "$t"\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const call = (extra) =>
      runDirectGrok({
        mode: "reason",
        args: ["--target", dir, "--task", "think", ...extra],
        cwd: dir,
        env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
        scriptsDir,
        python: "python3",
      }).envelopeText;
    // --web: the D-WEB tool set must be in the allowlist (else it runs ungrounded).
    const withWeb = call(["--web"]);
    for (const t of ["web_search", "web_fetch", "open_page", "open_page_with_find"]) {
      assert.match(withWeb, new RegExp(t), `--web must allowlist ${t}: ${withWeb}`);
    }
    // No --web: web tools must NOT be present.
    const noWeb = call([]);
    assert.doesNotMatch(noWeb, /web_search/, noWeb);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok web flag last-wins both orders (split and equals); verify stays hermetic", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-weblast-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Echo tools + disable-web-search so both on/off are observable.
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nt=""\ndws=0\nwhile [ $# -gt 0 ]; do\n  if [ "$1" = "--tools" ]; then t="$2"; fi\n  if [ "$1" = "--disable-web-search" ]; then dws=1; fi\n  shift\ndone\nprintf '{"result":"tools=%s dws=%s"}\\n' "$t" "$dws"\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const call = (mode, extra) =>
      JSON.parse(
        runDirectGrok({
          mode,
          args: ["--target", dir, "--task", "check", ...extra],
          cwd: dir,
          env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
          scriptsDir,
          python: "python3",
        }).envelopeText
      );
    // --web then --no-web => off (last wins).
    const webThenNo = call("reason", ["--web", "--no-web"]);
    assert.doesNotMatch(webThenNo.response.text, /web_search/, webThenNo.response.text);
    assert.match(webThenNo.response.text, /dws=1/);
    assert.equal(webThenNo.policy.webAccess, false);
    // --no-web then --web => on (last wins).
    const noThenWeb = call("reason", ["--no-web", "--web"]);
    assert.match(noThenWeb.response.text, /web_search/, noThenWeb.response.text);
    assert.match(noThenWeb.response.text, /dws=0/);
    assert.equal(noThenWeb.policy.webAccess, true);
    // Equals forms of both orders.
    const eqOff = call("reason", ["--web=1", "--no-web=1"]);
    assert.equal(eqOff.policy.webAccess, false);
    assert.doesNotMatch(eqOff.response.text, /web_search/);
    const eqOn = call("reason", ["--no-web=", "--web="]);
    assert.equal(eqOn.policy.webAccess, true);
    assert.match(eqOn.response.text, /web_search/);
    // Verify remains hermetic even when last flag is --web.
    const verifyLastWeb = call("verify", ["--no-web", "--web"]);
    assert.equal(verifyLastWeb.policy.webAccess, false);
    assert.doesNotMatch(verifyLastWeb.response.text, /web_search/);
    assert.match(verifyLastWeb.response.text, /dws=1/);
    assert.ok(
      verifyLastWeb.warnings.some((w) => /hermetic/i.test(w) && /--web ignored/.test(w)),
      `verify must still warn --web ignored: ${JSON.stringify(verifyLastWeb.warnings)}`
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok forces hermetic verify: --web is ignored (no web tools, --disable-web-search)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-hermetic-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Echo the tool allowlist and whether --disable-web-search was passed.
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nt=""\ndws=0\nwhile [ $# -gt 0 ]; do\n  if [ "$1" = "--tools" ]; then t="$2"; fi\n  if [ "$1" = "--disable-web-search" ]; then dws=1; fi\n  shift\ndone\nprintf '{"result":"tools=%s dws=%s"}\\n' "$t" "$dws"\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const call = (mode, extra) =>
      JSON.parse(
        runDirectGrok({
          mode,
          args: ["--target", dir, "--task", "check", ...extra],
          cwd: dir,
          env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
          scriptsDir,
          python: "python3",
        }).envelopeText
      );
    // verify --web: web tools MUST NOT appear, --disable-web-search MUST be set,
    // and a warning must record that --web was ignored for hermetic verify.
    const verifyWeb = call("verify", ["--web"]);
    assert.doesNotMatch(verifyWeb.response.text, /web_search/, "verify must not allowlist web tools");
    assert.match(verifyWeb.response.text, /dws=1/, "verify must pass --disable-web-search");
    assert.equal(verifyWeb.policy.webAccess, false, "verify policy.webAccess must be false");
    assert.ok(
      verifyWeb.warnings.some((w) => /hermetic/i.test(w) && /--web ignored/.test(w)),
      `verify must warn --web ignored: ${JSON.stringify(verifyWeb.warnings)}`
    );
    // Non-verify mode with --web is unchanged (web tools enabled).
    const reasonWeb = call("reason", ["--web"]);
    assert.match(reasonWeb.response.text, /web_search/, "reason --web still enables web tools");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok classifies a wall-clock timeout as class:timeout (not tool-unavailable)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-timeout-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Sleep past the 1s --timeout so spawnSync kills it (ETIMEDOUT).
    fs.writeFileSync(fakeGrok, `#!/bin/sh\nsleep 3\nprintf '{"result":"done"}\\n'\n`);
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const env = JSON.parse(
      runDirectGrok({
        mode: "reason",
        args: ["--target", dir, "--task", "x", "--timeout", "1"],
        cwd: dir,
        env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
        scriptsDir,
        python: "python3",
      }).envelopeText
    );
    assert.equal(env.status, "failure");
    assert.equal(env.error.class, "timeout", "timed-out direct run must classify as timeout");
    assert.equal(env.error.detail.timedOut, true);
    assert.equal(env.grok.stopReason, "timeout");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok forwards --max-turns to the installed CLI", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-mt-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Echo whether --max-turns <n> reached the CLI argv.
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nmt="none"\nwhile [ $# -gt 0 ]; do\n  if [ "$1" = "--max-turns" ]; then mt="$2"; fi\n  shift\ndone\nprintf '{"result":"maxturns=%s"}\\n' "$mt"\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const call = (extra) =>
      runDirectGrok({
        mode: "code",
        args: ["--target", dir, "--base", "HEAD", "--task", "x", ...extra],
        cwd: dir,
        env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
        scriptsDir,
        python: "python3",
      }).envelopeText;
    assert.match(call(["--max-turns", "5"]), /maxturns=5/, "valid --max-turns must be forwarded");
    assert.match(call(["--max-turns=7"]), /maxturns=7/, "equals form must be forwarded");
    // No --max-turns -> flag absent (not "0"/garbage).
    assert.match(call([]), /maxturns=none/);
    // Invalid / out-of-range values are not forwarded (no bogus cap).
    assert.match(call(["--max-turns", "0"]), /maxturns=none/);
    assert.match(call(["--max-turns", "abc"]), /maxturns=none/);
    assert.match(call(["--max-turns", "100001"]), /maxturns=none/); // > _MAX_TURNS
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok honors --worktree only for verify, not for code (consent safety)", () => {
  // SECURITY: `code --target <A> --worktree <B>` passes the direct-consent gate on
  // A but must NOT point the CLI at B (B never recorded consent). --worktree is a
  // verify-only flag; for code it is ignored and the cwd stays --target (A).
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-wtsafe-"));
  try {
    const repoA = path.join(dir, "A");
    const repoB = path.join(dir, "B");
    fs.mkdirSync(repoA);
    fs.mkdirSync(repoB);
    const fakeGrok = path.join(dir, "fake-grok.sh");
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nc=""\nwhile [ $# -gt 0 ]; do\n  if [ "$1" = "--cwd" ]; then c="$2"; fi\n  shift\ndone\nprintf '{"result":"cwd=%s"}\\n' "$c"\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const call = (mode) =>
      runDirectGrok({
        mode,
        args: ["--target", repoA, "--worktree", repoB, "--base", "HEAD", "--task", "x"],
        cwd: dir,
        env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
        scriptsDir,
        python: "python3",
      }).envelopeText;
    // code: --worktree ignored -> cwd is the consented --target A.
    assert.match(call("code"), new RegExp(`cwd=${esc(path.resolve(repoA))}`), "code must run in --target");
    assert.doesNotMatch(call("code"), new RegExp(`cwd=${esc(path.resolve(repoB))}"`));
    // verify: --worktree honored -> cwd is B (the retained worktree to inspect).
    assert.match(call("verify"), new RegExp(`cwd=${esc(path.resolve(repoB))}`), "verify must run in --worktree");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("default adversarial-review in a direct workspace is NOT broken by the --schema refusal", () => {
  // Regression: prepareReviewishArgs auto-injects the default findings schema; under
  // runMode=direct that must be SKIPPED, or the new --schema refusal rejects every
  // default `/grok:adversarial-review` even though the user passed no --schema.
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-arev-direct-"));
  const pluginData = path.join(cwd, "pdata");
  setRunMode(cwd, "direct", { CLAUDE_PLUGIN_DATA: pluginData });
  const fakeGrok = path.join(cwd, "fake-grok.sh");
  fs.writeFileSync(fakeGrok, `#!/bin/sh\nprintf '%s\\n' '{"result":"reviewed"}'\n`);
  fs.chmodSync(fakeGrok, 0o755);
  try {
    const res = runCompanion(["adversarial-review", "--target", "."], {
      cwd,
      env: {
        CLAUDE_PLUGIN_DATA: pluginData,
        GROK_AGENT_BINARY: fakeGrok,
        GROK_COMPANION_EXECUTION_CONTEXT: "foreground",
      },
    });
    const all = `${res.stdout}\n${res.stderr}`;
    assert.doesNotMatch(all, /--schema requires hardened/, `must not refuse: ${all}`);
    assert.equal(res.code, 0, `direct adversarial-review should run; ${all}`);
    assert.match(res.stdout, /reviewed/, "the installed CLI actually ran");
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("runDirectGrok refuses --schema in direct mode (no unvalidated structured output)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-schema-"));
  try {
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    for (const extra of [["--schema", "s.json"], ["--schema=s.json"]]) {
      const res = runDirectGrok({
        mode: "reason",
        args: ["--target", dir, "--task", "answer", ...extra],
        cwd: dir,
        env: { ...process.env, GROK_AGENT_BINARY: "/bin/true" },
        scriptsDir,
        python: "python3",
      });
      assert.equal(res.code, 1, `must refuse ${extra.join(" ")}`);
      const env = JSON.parse(res.envelopeText);
      assert.equal(env.status, "failure");
      assert.match(env.error?.message || "", /--schema/);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok refuses --input/--rules-file in direct mode (no silent drop)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-reason-"));
  try {
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    for (const extra of [["--input", "art.md"], ["--rules-file", "rules.md"], ["--input=art.md"]]) {
      const res = runDirectGrok({
        mode: "reason",
        args: ["--target", dir, "--task", "think", ...extra],
        cwd: dir,
        env: { ...process.env, GROK_AGENT_BINARY: "/bin/true" },
        scriptsDir,
        python: "python3",
      });
      assert.equal(res.code, 1, `must refuse ${extra.join(" ")}`);
      const env = JSON.parse(res.envelopeText);
      assert.equal(env.status, "failure");
      assert.match(env.error?.message || "", /--input|--rules-file/);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("resolveDirectTimeoutSeconds: per-mode defaults, override, junk, clamp", () => {
  assert.equal(resolveDirectTimeoutSeconds([], "code"), 3600);
  assert.equal(resolveDirectTimeoutSeconds([], "verify"), 1800);
  assert.equal(resolveDirectTimeoutSeconds([], "reason"), 900);
  assert.equal(resolveDirectTimeoutSeconds([], "review"), 900);
  assert.equal(resolveDirectTimeoutSeconds([], "adversarial-review"), 900);
  assert.equal(resolveDirectTimeoutSeconds([], "unknown-mode"), 900);
  assert.equal(resolveDirectTimeoutSeconds(["--timeout", "120"], "code"), 120);
  assert.equal(resolveDirectTimeoutSeconds(["--timeout=45"], "code"), 45);
  // junk / non-positive -> per-mode default
  assert.equal(resolveDirectTimeoutSeconds(["--timeout", "0"], "code"), 3600);
  // "--timeout -5" hits the flag-rejection branch (value starts with "-");
  // the equals form exercises the parsed n<=0 branch directly.
  assert.equal(resolveDirectTimeoutSeconds(["--timeout", "-5"], "code"), 3600);
  assert.equal(resolveDirectTimeoutSeconds(["--timeout=-5"], "code"), 3600);
  assert.equal(resolveDirectTimeoutSeconds(["--timeout", "abc"], "verify"), 1800);
  // clamped to the 7-day ceiling
  assert.equal(
    resolveDirectTimeoutSeconds(["--timeout", String(99 * 24 * 3600)], "code"),
    7 * 24 * 3600
  );
});

test("runDirectGrok stages the prompt file with private 0600 perms", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-promptperm-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Echo the octal mode of the --prompt-file so we can assert it end to end.
    // Use python's os.stat for portability (GNU `stat -f` means --file-system,
    // not a format string, so a shell `stat -f`/`stat -c` fallback is unreliable).
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\npf=""\nwhile [ $# -gt 0 ]; do\n  if [ "$1" = "--prompt-file" ]; then pf="$2"; fi\n  shift\ndone\nm=$(python3 -c 'import os,sys; print("%o" % (os.stat(sys.argv[1]).st_mode & 0o777))' "$pf")\nprintf '{"result":"mode=%s"}\\n' "$m"\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "secret prompt body"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    assert.match(res.envelopeText, /mode=600/, `prompt file must be 0600: ${res.envelopeText}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok honors --timeout and classifies a hung CLI as timed out", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-timeout-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Sleeps far longer than the 1s --timeout; the spawn must kill it.
    fs.writeFileSync(fakeGrok, `#!/bin/sh\nsleep 30\nprintf '{"result":"done"}\\n'\n`);
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x", "--timeout", "1"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    assert.equal(res.code, 1, `timed-out run must exit nonzero: ${res.envelopeText}`);
    const env = JSON.parse(res.envelopeText);
    assert.equal(env.status, "failure");
    assert.match(env.error?.message || "", /timeout/i);
    assert.equal(env.error?.detail?.timedOut, true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("result resolves direct-<timestamp> runId via the job index", () => {
  assert.ok(DIRECT_RUN_ID_RE.test(DIRECT_ID));
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };
  const job = createJob(cwd, { kind: "review", mode: "review", runMode: "direct" }, envBase);
  updateJob(cwd, job.id, { runId: DIRECT_ID, status: "success" }, envBase);
  const payload = JSON.stringify({
    status: "success",
    mode: "review",
    runId: DIRECT_ID,
    response: { text: "direct-job-output" },
  });
  storeJobStdout(cwd, job.id, `${payload}\n`, envBase);

  // Empty fake wrapper: result must not need the wrapper at all.
  const { env: fakeEnv, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["result", DIRECT_ID], {
      cwd,
      env: { ...fakeEnv, CLAUDE_PLUGIN_DATA: pluginData },
    });
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    assert.match(res.stdout, /direct-job-output/);
    assert.match(res.stdout, new RegExp(DIRECT_ID));
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("handoff --run-id direct-* refuses before wrapper spawn", () => {
  const cwd = tempCwd();
  // Empty responses: if wrapper were spawned, unregistered mode exits 2.
  const { env, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["handoff", "--run-id", DIRECT_ID], {
      cwd,
      env: { ...env, CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") },
    });
    assert.equal(res.code, 1, `expected exit 1; got ${res.code}; stderr: ${res.stderr}`);
    assert.ok(
      res.stderr.includes(DIRECT_NO_HANDOFF_MSG),
      `stderr must contain DIRECT_NO_HANDOFF_MSG; got: ${res.stderr}`
    );
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "handoff direct-id refuse must not spawn the wrapper"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("status --run-id direct-* refuses with the same shared message", () => {
  const cwd = tempCwd();
  const { env, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["status", "--run-id", DIRECT_ID], {
      cwd,
      env: { ...env, CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") },
    });
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.ok(
      res.stderr.includes(DIRECT_NO_HANDOFF_MSG),
      `stderr must contain DIRECT_NO_HANDOFF_MSG; got: ${res.stderr}`
    );
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "status direct-id refuse must not spawn the wrapper"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("DIRECT_NO_HANDOFF_MSG is the single shared refusal string", () => {
  assert.equal(typeof DIRECT_NO_HANDOFF_MSG, "string");
  assert.match(DIRECT_NO_HANDOFF_MSG, /direct-mode runs have no hardened run state/);
  assert.match(DIRECT_NO_HANDOFF_MSG, /setup --run-mode hardened/);
});

test("rawRunIdFlag first valid wins (direct refusal must not be hidden)", () => {
  assert.equal(
    rawRunIdFlag(["status", "--run-id", DIRECT_ID, "--run-id", "20260717T120000Z-abcdef"]),
    DIRECT_ID
  );
  assert.equal(
    rawRunIdFlag(["status", "--run-id=direct-1", "--run-id=direct-2"]),
    "direct-1"
  );
  // Invalid bare first occurrence is skipped; next valid wins as first valid.
  assert.equal(
    rawRunIdFlag(["status", "--run-id", "--pretty", "--run-id", DIRECT_ID]),
    DIRECT_ID
  );
  assert.equal(isDirectHandoffRequest("status", ["--run-id", DIRECT_ID]), true);
  assert.equal(
    isDirectHandoffRequest("status", [
      "--run-id",
      DIRECT_ID,
      "--run-id",
      "20260717T120000Z-abcdef",
    ]),
    true,
    "later hardened id must not hide earlier direct-* for refusal"
  );
});
