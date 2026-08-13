// plugin/scripts/tests/cli-defaults.test.mjs
//
// Dual-language SSOT + direct/companion argv contracts for grok-4.6 default,
// reasoning-effort last-wins, invalid-effort fail-closed, and --no-plan
// default vs --plan opt-out. A second hardcoded default-model or effort
// vocabulary in production sources must fail this file.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  CLI_DEFAULTS_SSOT_PATH,
  defaultModel,
  isSameModelFamily,
  loadCliDefaults,
  noPlanDefault,
  parseCliDefaultsDoc,
  parseReasoningEffort,
  reasoningEffortValues,
  resolveNoPlan,
  resolveReasoningEffort,
  resolveRequestedModel,
} from "../lib/cli-defaults.mjs";
import { runDirectGrok } from "../lib/direct-grok.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXPECTED_SSOT = path.resolve(HERE, "../../references/grok-cli-defaults.json");
const SCRIPTS_LIB = path.resolve(HERE, "../lib");
const COMPANION = path.resolve(HERE, "../grok-companion.mjs");

test("cli-defaults SSOT is plugin/references/grok-cli-defaults.json and default is grok-4.6", () => {
  assert.equal(CLI_DEFAULTS_SSOT_PATH, EXPECTED_SSOT);
  assert.ok(fs.existsSync(CLI_DEFAULTS_SSOT_PATH), `missing ${CLI_DEFAULTS_SSOT_PATH}`);
  const doc = loadCliDefaults();
  assert.equal(doc.defaultModel, "grok-4.6");
  assert.equal(defaultModel(), "grok-4.6");
  assert.deepEqual(reasoningEffortValues(), ["low", "medium", "high", "xhigh"]);
  assert.equal(doc.noPlanDefault, true);
});

test("parseReasoningEffort accepts vocabulary and fails closed on blank/unknown", () => {
  for (const v of ["low", "medium", "high", "xhigh", "HIGH", " Xhigh "]) {
    assert.equal(parseReasoningEffort(v), v.trim().toLowerCase());
  }
  for (const bad of ["", "   ", "turbo", "max", null, undefined]) {
    assert.throws(() => parseReasoningEffort(bad), /effort|usage/i);
  }
});

test("resolveReasoningEffort last-wins across split, equals, and --effort alias", () => {
  assert.equal(resolveReasoningEffort(["--reasoning-effort", "low"]), "low");
  assert.equal(resolveReasoningEffort(["--reasoning-effort=medium"]), "medium");
  assert.equal(resolveReasoningEffort(["--effort", "high"]), "high");
  assert.equal(
    resolveReasoningEffort(["--reasoning-effort", "low", "--effort=xhigh"]),
    "xhigh"
  );
  assert.equal(resolveReasoningEffort(["--effort", "high", "--reasoning-effort", "low"]), "low");
  assert.equal(resolveReasoningEffort(["code", "--task", "x"]), null);
});

test("resolveReasoningEffort invalid value fails closed (no silent drop)", () => {
  assert.throws(
    () => resolveReasoningEffort(["--reasoning-effort", "turbo"]),
    /effort|usage/i
  );
  assert.throws(() => resolveReasoningEffort(["--effort="]), /effort|usage/i);
  // Argparse type-checks every occurrence; last-wins must not hide an earlier invalid.
  assert.throws(
    () => resolveReasoningEffort(["--reasoning-effort", "turbo", "--effort", "high"]),
    /effort|usage/i
  );
  assert.throws(
    () => resolveReasoningEffort(["--effort=", "--reasoning-effort", "high"]),
    /effort|usage/i
  );
});

test("resolveNoPlan defaults on and --plan last-wins opt-out", () => {
  assert.equal(resolveNoPlan([]), true);
  assert.equal(resolveNoPlan(["--no-plan"]), true);
  assert.equal(resolveNoPlan(["--plan"]), false);
  assert.equal(resolveNoPlan(["--plan", "--no-plan"]), true);
  assert.equal(resolveNoPlan(["--no-plan", "--plan"]), false);
});

test("resolveNoPlan valued --plan=/--no-plan= fails closed (wrapper argparse parity)", () => {
  assert.throws(() => resolveNoPlan(["--plan=true"]), /plan|usage/i);
  assert.throws(() => resolveNoPlan(["--plan=false"]), /plan|usage/i);
  assert.throws(() => resolveNoPlan(["--no-plan="]), /plan|usage/i);
});

test("resolveRequestedModel omits to default; blank --model fails closed", () => {
  assert.equal(resolveRequestedModel(["code", "--task", "x"]), defaultModel());
  assert.equal(resolveRequestedModel(["--model", "grok-4.5"]), "grok-4.5");
  assert.throws(() => resolveRequestedModel(["--model="]), /model|usage/i);
  assert.throws(() => resolveRequestedModel(["--model", "   "]), /model|usage/i);
  // Argparse type-checks every occurrence; last-wins must not hide an earlier blank.
  assert.throws(
    () => resolveRequestedModel(["--model=", "--model", "grok-4.5"]),
    /model|usage/i
  );
  assert.throws(
    () => resolveRequestedModel(["--model", "  ", "--model", "grok-4.5"]),
    /model|usage/i
  );
});

test("runDirectGrok default argv requests grok-4.6, pins --no-plan, omits effort", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-46-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh
model=none
plan=no
effort=none
while [ $# -gt 0 ]; do
  if [ "$1" = "--model" ]; then model="$2"; fi
  if [ "$1" = "--no-plan" ]; then plan=yes; fi
  if [ "$1" = "--reasoning-effort" ]; then effort="$2"; fi
  shift
done
printf '{"result":"model=%s plan=%s effort=%s"}\\n' "$model" "$plan" "$effort"
`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(HERE, "../../wrapper/scripts");
    const envText = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    }).envelopeText;
    assert.match(envText, /model=grok-4\.6/, envText);
    assert.match(envText, /plan=yes/, envText);
    assert.match(envText, /effort=none/, envText);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok explicit grok-4.5, effort last-wins, and --plan omits --no-plan", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-effort-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh
model=none
plan=no
effort=none
while [ $# -gt 0 ]; do
  if [ "$1" = "--model" ]; then model="$2"; fi
  if [ "$1" = "--no-plan" ]; then plan=yes; fi
  if [ "$1" = "--reasoning-effort" ]; then effort="$2"; fi
  shift
done
printf '{"result":"model=%s plan=%s effort=%s"}\\n' "$model" "$plan" "$effort"
`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(HERE, "../../wrapper/scripts");
    const call = (extra) =>
      runDirectGrok({
        mode: "reason",
        args: ["--target", dir, "--task", "x", ...extra],
        cwd: dir,
        env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
        scriptsDir,
        python: "python3",
      }).envelopeText;
    assert.match(call(["--model", "grok-4.5"]), /model=grok-4\.5/);
    assert.match(call(["--reasoning-effort", "low", "--effort=xhigh"]), /effort=xhigh/);
    assert.match(call(["--plan"]), /plan=no/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok blank --model= fails closed as usage-error", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-blank-model-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    fs.writeFileSync(fakeGrok, `#!/bin/sh\nprintf '{"result":"should-not-run"}\\n'\n`);
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(HERE, "../../wrapper/scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x", "--model="],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    const env = JSON.parse(res.envelopeText);
    assert.notEqual(res.code, 0);
    assert.equal(env.status, "failure");
    assert.equal(env.error.class, "usage-error");
    assert.doesNotMatch(res.envelopeText, /should-not-run/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok invalid effort fails closed as usage-error", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-bad-effort-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    fs.writeFileSync(fakeGrok, `#!/bin/sh\nprintf '{"result":"should-not-run"}\\n'\n`);
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(HERE, "../../wrapper/scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x", "--reasoning-effort", "turbo"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    const env = JSON.parse(res.envelopeText);
    assert.notEqual(res.code, 0);
    assert.equal(env.status, "failure");
    assert.equal(env.error.class, "usage-error");
    assert.doesNotMatch(res.envelopeText, /should-not-run/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("isSameModelFamily matches the hyphen-boundary rule (not startswith)", () => {
  assert.equal(isSameModelFamily("grok-4.6", "grok-4.6"), true);
  assert.equal(isSameModelFamily("grok-4.6-build", "grok-4.6"), true);
  assert.equal(isSameModelFamily("grok-4.5", "grok-4.6"), false);
  assert.equal(isSameModelFamily("grok-4.5-build", "grok-4.6"), false);
  assert.equal(isSameModelFamily("grok-4.5", "grok-4"), false);
});

test("noPlanDefault is the SSOT boolean, not a hardcoded true check", () => {
  const doc = loadCliDefaults();
  assert.equal(typeof doc.noPlanDefault, "boolean");
  assert.equal(noPlanDefault(), doc.noPlanDefault);
  assert.equal(resolveNoPlan([]), doc.noPlanDefault);
  const flipped = parseCliDefaultsDoc({ ...doc, noPlanDefault: false });
  assert.equal(flipped.noPlanDefault, false);
  assert.throws(
    () => parseCliDefaultsDoc({ ...doc, noPlanDefault: "yes" }),
    /noPlanDefault/
  );
  const src = fs.readFileSync(path.join(SCRIPTS_LIB, "cli-defaults.mjs"), "utf8");
  assert.doesNotMatch(src, /noPlanDefault !== true/);
  assert.doesNotMatch(src, /noPlanDefault must be true/);
});

test("runDirectGrok fails closed as model-unavailable when modelUsage is another family", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-family-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh
printf '%s\\n' '{"result":"should-not-succeed","stopReason":"end_turn","modelUsage":{"grok-4.5":{"inputTokens":1}}}'
`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(HERE, "../../wrapper/scripts");
    const res = runDirectGrok({
      mode: "review",
      args: ["--target", dir, "--task", "x"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    const env = JSON.parse(res.envelopeText);
    assert.notEqual(res.code, 0);
    assert.equal(env.status, "failure");
    assert.equal(env.error.class, "model-unavailable");
    assert.doesNotMatch(res.envelopeText, /should-not-succeed/);
    assert.equal(env.policy.model, defaultModel());
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("DRY: production Node sources do not retype default model or effort vocabulary", () => {
  const files = [
    ...fs.readdirSync(SCRIPTS_LIB).filter((n) => n.endsWith(".mjs")).map((n) => path.join(SCRIPTS_LIB, n)),
    COMPANION,
  ];
  const allowed = new Set(["cli-defaults.mjs"]);
  const hits = [];
  const effortRe = /["']low["']\s*,\s*["']medium["']\s*,\s*["']high["']\s*,\s*["']xhigh["']/;
  for (const file of files) {
    const name = path.basename(file);
    if (allowed.has(name)) continue;
    const text = fs.readFileSync(file, "utf8");
    if (text.includes("grok-4.6")) hits.push(`${name}: grok-4.6 literal`);
    if (effortRe.test(text)) hits.push(`${name}: effort vocabulary`);
    if (text.includes('|| "grok-4.5"') || text.includes("|| 'grok-4.5'")) {
      hits.push(`${name}: grok-4.5 fallback default`);
    }
  }
  assert.deepEqual(hits, [], `second copy of SSOT contracts: ${hits.join("; ")}`);
});
