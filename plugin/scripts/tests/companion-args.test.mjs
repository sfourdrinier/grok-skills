// plugin/scripts/tests/companion-args.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  flagValue,
  hasFlagOrEquals,
  resolveWebFlag,
  stripFlags,
} from "../lib/companion-args.mjs";

test("hasFlagOrEquals matches split AND equals forms, not prefixes", () => {
  assert.equal(hasFlagOrEquals(["review", "--target", "."], "--target"), true);
  assert.equal(hasFlagOrEquals(["review", "--target=/repo"], "--target"), true);
  assert.equal(hasFlagOrEquals(["review", "--schema=/s.json"], "--schema"), true);
  assert.equal(hasFlagOrEquals(["review"], "--target"), false);
  // Must NOT match a longer flag that merely shares the prefix.
  assert.equal(hasFlagOrEquals(["--target-workspace=x"], "--target"), false);
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
