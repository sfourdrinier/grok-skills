// plugin/scripts/tests/companion-args.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { stripFlags } from "../lib/companion-args.mjs";

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
