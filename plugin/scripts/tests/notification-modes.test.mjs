import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatWebhookDisplay,
  isNotificationMode,
  NOTIFICATION_MODES,
  parseNotificationMode,
  parseWebhookUrl,
  shouldAttemptTerminalNotify,
} from "../lib/notification-modes.mjs";

test("NOTIFICATION_MODES is the single product set", () => {
  assert.deepEqual([...NOTIFICATION_MODES], ["off", "auto", "native", "webhook"]);
});

test("parseNotificationMode accepts known modes and rejects junk", () => {
  assert.equal(parseNotificationMode("auto"), "auto");
  assert.equal(parseNotificationMode("  WEBHOOK  "), "webhook");
  assert.equal(parseNotificationMode("telepathy"), null);
  assert.equal(isNotificationMode("native"), true);
  assert.equal(isNotificationMode("nope"), false);
});

test("parseWebhookUrl accepts http(s), clears empty, rejects other schemes", () => {
  assert.deepEqual(parseWebhookUrl("https://hooks.example.com/h"), {
    ok: true,
    url: "https://hooks.example.com/h",
  });
  assert.deepEqual(parseWebhookUrl("http://127.0.0.1:9/x"), {
    ok: true,
    url: "http://127.0.0.1:9/x",
  });
  assert.deepEqual(parseWebhookUrl(""), { ok: true, url: null });
  assert.deepEqual(parseWebhookUrl(null), { ok: true, url: null });
  assert.equal(parseWebhookUrl("file:///etc/passwd").ok, false);
  assert.equal(parseWebhookUrl("not a url").ok, false);
  assert.equal(parseWebhookUrl("ftp://example.com/x").ok, false);
});

test("shouldAttemptTerminalNotify honors skipNotify", () => {
  assert.equal(shouldAttemptTerminalNotify({}), true);
  assert.equal(shouldAttemptTerminalNotify({ skipNotify: false }), true);
  assert.equal(shouldAttemptTerminalNotify({ skipNotify: true }), false);
});

test("formatWebhookDisplay shows host only (no path secrets)", () => {
  assert.equal(formatWebhookDisplay(null), "none");
  assert.equal(
    formatWebhookDisplay("https://hooks.slack.com/services/T00/B00/secret-token"),
    "https://hooks.slack.com"
  );
  assert.equal(
    formatWebhookDisplay("https://user:pass@discord.com/api/webhooks/1/xyz?x=1"),
    "https://discord.com"
  );
  assert.doesNotMatch(
    formatWebhookDisplay("https://hooks.slack.com/services/T00/B00/secret-token"),
    /secret-token/
  );
});
