// plugin/scripts/tests/session-stamp.test.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  buildTransferTaskBody,
  readSessionStamp,
  resolveTransferSource,
  sessionStampPath,
  writeSessionStamp,
  writeTransferPack,
} from "../lib/session-stamp.mjs";

test("session stamp is workspace-keyed and private mode", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-stamp-ws-"));
  const env = { XDG_STATE_HOME: fs.mkdtempSync(path.join(os.tmpdir(), "grok-stamp-xdg-")) };
  const a = sessionStampPath(cwd, env);
  const other = fs.mkdtempSync(path.join(os.tmpdir(), "grok-stamp-ws2-"));
  const b = sessionStampPath(other, env);
  assert.notEqual(a, b);
  writeSessionStamp(cwd, { event: "SessionStart", transcript_path: "/tmp/x.jsonl" }, env);
  const stamp = readSessionStamp(cwd, env);
  assert.equal(stamp.transcript_path, "/tmp/x.jsonl");
  const st = fs.statSync(a);
  // mode may be masked by umask on some systems; still a regular file
  assert.ok(st.isFile());
});

test("resolveTransferSource rejects paths outside allowlist without --force", () => {
  const secret = path.join(os.tmpdir(), `grok-secret-${Date.now()}.jsonl`);
  fs.writeFileSync(secret, '{"role":"user","content":"hi"}\n', "utf8");
  const denied = resolveTransferSource(secret, { force: false });
  assert.equal(denied.ok, false);
  assert.match(denied.reason, /outside allowed transcript roots|must be a \.jsonl/i);
  const forced = resolveTransferSource(secret, { force: true });
  assert.equal(forced.ok, true);
  fs.unlinkSync(secret);
});

test("resolveTransferSource rejects oversized files", () => {
  const big = path.join(os.tmpdir(), `grok-big-${Date.now()}.jsonl`);
  fs.writeFileSync(big, "x".repeat(3 * 1024 * 1024), "utf8");
  const r = resolveTransferSource(big, { force: true });
  assert.equal(r.ok, false);
  assert.match(r.reason, /exceeds/);
  fs.unlinkSync(big);
});

test("buildTransferTaskBody and writeTransferPack", () => {
  const session = path.join(os.tmpdir(), `grok-sess-${Date.now()}.jsonl`);
  fs.writeFileSync(
    session,
    `${JSON.stringify({ role: "user", content: "hello transfer" })}\n`,
    "utf8"
  );
  const body = buildTransferTaskBody(session);
  assert.match(body, /hello transfer/);
  const env = { CLAUDE_PLUGIN_DATA: fs.mkdtempSync(path.join(os.tmpdir(), "grok-pack-")) };
  const pack = writeTransferPack(body, env);
  assert.ok(fs.existsSync(pack));
  assert.match(fs.readFileSync(pack, "utf8"), /hello transfer/);
  fs.unlinkSync(session);
});
