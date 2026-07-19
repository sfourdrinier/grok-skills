// plugin/scripts/tests/companion-args.test.mjs
import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";

import {
  dropValueFlags,
  firstFlagValue,
  flagOccurrences,
  flagValue,
  hasFlagOrEquals,
  isFlagToken,
  resolveWebFlag,
  stripFlags,
  stripValueFlag,
} from "../lib/companion-args.mjs";

test("hasFlagOrEquals matches split AND equals forms, not prefixes", () => {
  assert.equal(hasFlagOrEquals(["review", "--target", "."], "--target"), true);
  assert.equal(hasFlagOrEquals(["review", "--target=/repo"], "--target"), true);
  assert.equal(hasFlagOrEquals(["review", "--schema=/s.json"], "--schema"), true);
  assert.equal(hasFlagOrEquals(["review"], "--target"), false);
  // Must NOT match a longer flag that merely shares the prefix.
  assert.equal(hasFlagOrEquals(["--target-workspace=x"], "--target"), false);
});

test("isFlagToken treats flags as flags and '-' as a value sentinel", () => {
  assert.equal(isFlagToken("--target"), true);
  assert.equal(isFlagToken("-x"), true);
  assert.equal(isFlagToken("-"), false);
  assert.equal(isFlagToken("value"), false);
  assert.equal(isFlagToken(undefined), false);
});

test("stripFlags captures split AND equals forms of --base / --run-mode", () => {
  const split = stripFlags(["review", "--base", "main", "--run-mode", "direct"]);
  assert.equal(split.base, "main");
  assert.equal(split.runMode, "direct");
  assert.ok(!split.args.includes("--base") && !split.args.includes("main"));

  // Equals form: the hardened wrapper's argparse accepts it, so the companion must
  // too, or direct review/code silently drops the base comparison / posture switch.
  const eq = stripFlags(["review", "--base=main", "--run-mode=direct"]);
  assert.equal(eq.base, "main", "equals-form --base must be captured");
  assert.equal(eq.runMode, "direct", "equals-form --run-mode must be captured");
  assert.ok(!eq.args.some((a) => a.startsWith("--base=")), "leftover --base= must be stripped");
  assert.ok(!eq.args.some((a) => a.startsWith("--run-mode=")));
});

test("stripFlags leaves unrelated args intact and handles an empty equals value", () => {
  const r = stripFlags(["code", "--target", ".", "--task", "x"]);
  assert.equal(r.base, null);
  assert.equal(r.runMode, null);
  assert.deepEqual(r.args, ["code", "--target", ".", "--task", "x"]);
  // An empty equals value is captured as "" (falsy) and the token is stripped;
  // downstream re-attach guards skip a falsy base.
  const e = stripFlags(["review", "--base="]);
  assert.equal(e.base, "");
  assert.ok(!e.args.some((a) => a.startsWith("--base=")));
});

test("stripFlags does not consume a following flag as a value-bearing flag's value", () => {
  // Regression: `--run-mode --integration auto` used to swallow `--integration`
  // as the run-mode value and drop the real integration token.
  const r = stripFlags([
    "code",
    "--run-mode",
    "--integration",
    "auto",
    "--base",
    "--task",
    "x",
  ]);
  assert.equal(r.runMode, null, "following flag must not become --run-mode value");
  assert.equal(r.integration, "auto", "--integration auto must still be captured");
  assert.equal(r.base, null, "following flag must not become --base value");
  assert.deepEqual(r.args, ["code", "--task", "x"]);
});

test("flagValue last-wins for split and equals forms (argparse parity)", () => {
  // Single source for companion/direct argv values: last occurrence wins, matching
  // the wrapper's argparse (and parseTargetFlag / consent keying).
  assert.equal(flagValue(["--task", "first", "--task", "second"], "--task"), "second");
  assert.equal(flagValue(["--task=first", "--task=second"], "--task"), "second");
  assert.equal(flagValue(["--task", "first", "--task=second"], "--task"), "second");
  assert.equal(flagValue(["--task=first", "--task", "second"], "--task"), "second");
  assert.equal(flagValue(["--timeout", "10", "--timeout=20"], "--timeout"), "20");
  // Do not consume a following FLAG as the value (parity with parseTargetFlag).
  assert.equal(flagValue(["--task", "--target"], "--task"), null);
  assert.equal(flagValue(["--task"], "--task"), null);
  assert.equal(flagValue(["code", "--target", "."], "--task"), null);
  assert.equal(flagValue(null, "--task"), null);
});

test("flagValue keeps prior good when a later bare invalid duplicate appears", () => {
  // Regression: last occurrence was invalid (next token is a flag) and used to
  // wipe a previously captured good value to null.
  assert.equal(
    flagValue(["--task", "good", "--task", "--target", "."], "--task"),
    "good"
  );
  assert.equal(
    flagValue(["--timeout=30", "--timeout", "--web"], "--timeout"),
    "30"
  );
  assert.equal(
    flagValue(["--run-id=direct-1", "--run-id", "--pretty"], "--run-id"),
    "direct-1"
  );
});

test("firstFlagValue is first-wins (valid values only)", () => {
  assert.equal(
    firstFlagValue(["--run-id", "direct-1", "--run-id", "direct-2"], "--run-id"),
    "direct-1"
  );
  assert.equal(
    firstFlagValue(["--run-id=direct-1", "--run-id=direct-2"], "--run-id"),
    "direct-1"
  );
  // Skip an invalid bare first occurrence.
  assert.equal(
    firstFlagValue(["--run-id", "--pretty", "--run-id", "direct-9"], "--run-id"),
    "direct-9"
  );
  assert.equal(firstFlagValue(["--run-id", "--pretty"], "--run-id"), null);
});

test("flagOccurrences distinguishes valid split values from next flags", () => {
  const occ = flagOccurrences(
    ["--task", "a", "--task", "--target", "--task=b"],
    "--task"
  );
  assert.equal(occ.length, 3);
  assert.equal(occ[0].value, "a");
  assert.equal(occ[1].value, null);
  assert.equal(occ[2].value, "b");
  assert.equal(occ[2].form, "equals");
});

test("stripValueFlag / dropValueFlags never consume a following flag as a value", () => {
  const stripped = stripValueFlag(
    ["code", "--integration", "--target", ".", "rest"],
    "--integration"
  );
  assert.equal(stripped.value, null);
  assert.deepEqual(stripped.args, ["code", "--target", ".", "rest"]);

  const dropped = dropValueFlags(
    ["code", "--task", "--target", ".", "--task-file", "p", "--web"],
    ["--task", "--task-file"]
  );
  // --task had no valid value (next was --target); only the flag token drops.
  // --task-file p drops both tokens.
  assert.deepEqual(dropped, ["code", "--target", ".", "--web"]);
});

test("resolveWebFlag last occurrence of --web/--no-web decides (split and equals)", () => {
  assert.equal(resolveWebFlag([]), null);
  assert.equal(resolveWebFlag(["--web"]), true);
  assert.equal(resolveWebFlag(["--no-web"]), false);
  // Last occurrence wins either order.
  assert.equal(resolveWebFlag(["--web", "--no-web"]), false);
  assert.equal(resolveWebFlag(["--no-web", "--web"]), true);
  // Equals forms are accepted (parity with hasFlagOrEquals / wrapper argparse).
  assert.equal(resolveWebFlag(["--web=1", "--no-web=1"]), false);
  assert.equal(resolveWebFlag(["--no-web=", "--web="]), true);
  // Mixed split/equals still last-wins.
  assert.equal(resolveWebFlag(["--web", "--no-web="]), false);
  assert.equal(resolveWebFlag(["--no-web", "--web=true"]), true);
  // Prefix must not match longer flags.
  assert.equal(resolveWebFlag(["--web-search"]), null);
  assert.equal(resolveWebFlag(null), null);
});

// DRY contract: stripFlags must reuse stripValueFlag for value-bearing flags
// rather than re-implementing startsWith / next-token loops locally.
test("stripFlags reuses stripValueFlag (source SSOT)", () => {
  const src = fs.readFileSync(
    new URL("../lib/companion-args.mjs", import.meta.url),
    "utf8"
  );
  const body = src.slice(src.indexOf("export function stripFlags"));
  assert.match(body, /stripValueFlag\(/, "stripFlags must call stripValueFlag");
  // Value-bearing peel must not re-open a local startsWith equals loop.
  assert.doesNotMatch(
    body,
    /a\.startsWith\("--run-mode="\)/,
    "stripFlags must not locally startsWith --run-mode="
  );
  assert.doesNotMatch(
    body,
    /a\.startsWith\("--integration="\)/,
    "stripFlags must not locally startsWith --integration="
  );
  assert.doesNotMatch(
    body,
    /a\.startsWith\("--base="\)/,
    "stripFlags must not locally startsWith --base="
  );
});
