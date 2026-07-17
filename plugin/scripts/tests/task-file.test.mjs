// plugin/scripts/tests/task-file.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { stageTaskFile, injectTaskFile, stageStdinTaskFile } from "../lib/task-file.mjs";

test("stageTaskFile writes 0600 file and cleanup removes it", () => {
  const { taskPath, cleanup } = stageTaskFile("hello task");
  assert.equal(fs.readFileSync(taskPath, "utf8"), "hello task");
  assert.equal(fs.statSync(taskPath).mode & 0o777, 0o600);
  cleanup();
  assert.equal(fs.existsSync(taskPath), false);
});

test("injectTaskFile strips old task flags and appends staged file", () => {
  const { args, cleanup } = injectTaskFile(["code", "--task", "old", "--target", "."], "new text");
  assert.equal(args[0], "code");
  assert.ok(!args.includes("old"));
  const tf = args.indexOf("--task-file");
  assert.ok(tf > 0);
  assert.equal(fs.readFileSync(args[tf + 1], "utf8"), "new text");
  cleanup();
});

test("stageStdinTaskFile returns null when argv has no --task-file - sentinel", () => {
  assert.equal(stageStdinTaskFile(["code", "--target", "."]), null);
  assert.equal(stageStdinTaskFile(["code", "--task-file", "/tmp/x"]), null);
  assert.equal(stageStdinTaskFile(["code", "--task", "inline"]), null);
});
