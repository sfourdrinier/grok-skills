// plugin/scripts/lib/notify.mjs
//
// At-most-once notification *attempt* after a terminal companion run (design §11).
// Priority: no duplicate automatic attempts over guaranteed delivery.
// Not exactly-once. Operator re-attempt is PR5 only (force path later).
//
// Single owner of notified.json marker I/O and native/webhook adapters.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import path from "node:path";
import { URL } from "node:url";

const FILE_MODE = 0o600;
const NATIVE_TIMEOUT_MS = 5000;
const WEBHOOK_TIMEOUT_MS = 3000;
const TITLE = "Grok Skills";

const NOTIFY_MODES = new Set(["off", "auto", "native", "webhook"]);

/**
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {"foreground"|"background"}
 */
export function getExecutionContext(env = process.env) {
  const raw = (env.GROK_COMPANION_EXECUTION_CONTEXT ?? "").trim().toLowerCase();
  if (raw === "background") {
    return "background";
  }
  return "foreground";
}

/**
 * @param {{ notificationMode: string, executionContext: string, webhookUrl?: string|null }} opts
 * @returns {{ notify: boolean, reason: string }}
 */
export function shouldNotify({ notificationMode, executionContext, webhookUrl = null }) {
  const mode = NOTIFY_MODES.has(notificationMode) ? notificationMode : "off";
  if (mode === "off") {
    return { notify: false, reason: "mode-off" };
  }
  if (mode === "auto") {
    if (executionContext !== "background") {
      return { notify: false, reason: "auto-foreground" };
    }
    return { notify: true, reason: "auto-background" };
  }
  if (mode === "webhook") {
    if (!webhookUrl || !String(webhookUrl).trim()) {
      return { notify: false, reason: "webhook-url-missing" };
    }
    return { notify: true, reason: "webhook" };
  }
  // native
  return { notify: true, reason: "native" };
}

/**
 * Modes that may trigger notify after a live run finishes.
 * Never status/cleanup/setup/preflight/jobs/result/cancel/handoff alone.
 */
export const NOTIFY_ELIGIBLE_MODES = new Set([
  "review",
  "reason",
  "code",
  "verify",
  "adversarial-review",
]);

/**
 * Env for wrapper/python children: never forward execution context.
 * @param {NodeJS.ProcessEnv} [base]
 * @returns {NodeJS.ProcessEnv}
 */
export function wrapperChildEnv(base = process.env) {
  const out = { ...base };
  delete out.GROK_COMPANION_EXECUTION_CONTEXT;
  return out;
}

function nowIso() {
  return new Date().toISOString();
}

function writePrivate(filePath, content) {
  fs.writeFileSync(filePath, content, { encoding: "utf8", mode: FILE_MODE });
  try {
    fs.chmodSync(filePath, FILE_MODE);
  } catch {
    /* best-effort */
  }
}

function markerPathFor(runDir) {
  return path.join(runDir, "notified.json");
}

/**
 * Try exclusive create of pending marker. Returns false if already exists.
 * @param {string} markerPath
 * @returns {boolean}
 */
function createPendingMarker(markerPath) {
  const body = JSON.stringify(
    {
      state: "pending",
      attemptedAt: nowIso(),
      adapter: null,
      result: null,
    },
    null,
    2
  );
  try {
    fs.writeFileSync(markerPath, `${body}\n`, { encoding: "utf8", mode: FILE_MODE, flag: "wx" });
    try {
      fs.chmodSync(markerPath, FILE_MODE);
    } catch {
      /* best-effort */
    }
    return true;
  } catch (err) {
    if (err && (err.code === "EEXIST" || err.code === "EISDIR")) {
      return false;
    }
    throw err;
  }
}

function completeMarker(markerPath, patch) {
  let prior = {};
  try {
    prior = JSON.parse(fs.readFileSync(markerPath, "utf8"));
  } catch {
    prior = {};
  }
  const body = {
    state: "completed",
    attemptedAt: prior.attemptedAt ?? nowIso(),
    completedAt: nowIso(),
    adapter: patch.adapter ?? null,
    result: patch.result ?? "failed",
    detail: patch.detail ?? null,
  };
  writePrivate(markerPath, `${JSON.stringify(body, null, 2)}\n`);
  return body;
}

function escapeAppleScriptString(value) {
  return `"${String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

/**
 * @returns {{ ok: boolean, detail: string, adapter: string }}
 */
function sendNative(title, body) {
  if (process.platform === "darwin") {
    const script = `display notification ${escapeAppleScriptString(body)} with title ${escapeAppleScriptString(title)}`;
    const result = spawnSync("osascript", ["-e", script], {
      encoding: "utf8",
      timeout: NATIVE_TIMEOUT_MS,
      shell: false,
    });
    if (result.error) {
      return { ok: false, detail: result.error.message, adapter: "native" };
    }
    if (result.status !== 0) {
      return {
        ok: false,
        detail: (result.stderr || result.stdout || `exit ${result.status}`).trim(),
        adapter: "native",
      };
    }
    return { ok: true, detail: "osascript", adapter: "native" };
  }
  if (process.platform === "linux") {
    const result = spawnSync("notify-send", ["--", title, body], {
      encoding: "utf8",
      timeout: NATIVE_TIMEOUT_MS,
      shell: false,
    });
    if (result.error) {
      return { ok: false, detail: result.error.message, adapter: "native" };
    }
    if (result.status !== 0) {
      return {
        ok: false,
        detail: (result.stderr || result.stdout || `exit ${result.status}`).trim(),
        adapter: "native",
      };
    }
    return { ok: true, detail: "notify-send", adapter: "native" };
  }
  if (process.platform === "win32") {
    return { ok: false, detail: "windows-native-unsupported", adapter: "native" };
  }
  return { ok: false, detail: "native-unsupported-platform", adapter: "native" };
}

/**
 * @param {string} urlString
 * @param {object} payload
 * @returns {Promise<{ ok: boolean, detail: string, adapter: string }>}
 */
function sendWebhook(urlString, payload) {
  return new Promise((resolve) => {
    let url;
    try {
      url = new URL(urlString);
    } catch (err) {
      resolve({ ok: false, detail: `invalid-url: ${err.message}`, adapter: "webhook" });
      return;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      resolve({ ok: false, detail: "webhook-protocol-not-http(s)", adapter: "webhook" });
      return;
    }
    const lib = url.protocol === "https:" ? https : http;
    const data = Buffer.from(JSON.stringify(payload), "utf8");
    const req = lib.request(
      {
        protocol: url.protocol,
        hostname: url.hostname,
        port: url.port || (url.protocol === "https:" ? 443 : 80),
        path: `${url.pathname}${url.search}`,
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": data.length,
        },
        timeout: WEBHOOK_TIMEOUT_MS,
      },
      (res) => {
        res.resume();
        const code = res.statusCode ?? 0;
        if (code >= 200 && code < 300) {
          resolve({ ok: true, detail: `http-${code}`, adapter: "webhook" });
        } else {
          resolve({ ok: false, detail: `http-${code}`, adapter: "webhook" });
        }
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, detail: "webhook-timeout", adapter: "webhook" });
    });
    req.on("error", (err) => {
      resolve({ ok: false, detail: err.message, adapter: "webhook" });
    });
    req.write(data);
    req.end();
  });
}

/**
 * At-most-once attempt. Never throws to fail the job.
 *
 * @param {object} opts
 * @param {string} opts.runDir - absolute path to runs/<runId>
 * @param {string} opts.runId
 * @param {string} opts.mode - wrapper mode
 * @param {string} opts.lifecycle - e.g. completed/failed
 * @param {number} [opts.durationSeconds]
 * @param {string} opts.notificationMode
 * @param {string|null} [opts.webhookUrl]
 * @param {NodeJS.ProcessEnv} [opts.env]
 * @returns {Promise<{ attempted: boolean, sent: boolean, reason: string, detail?: string }>}
 */
export async function attemptNotify(opts) {
  const {
    runDir,
    runId,
    mode,
    lifecycle,
    durationSeconds = 0,
    notificationMode,
    webhookUrl = null,
    env = process.env,
  } = opts;

  try {
    if (!runDir || !runId) {
      return { attempted: false, sent: false, reason: "missing-run-dir-or-id" };
    }
    // Never create runs/<id>; only notify when the wrapper already materialised the dir.
    if (!fs.existsSync(runDir) || !fs.statSync(runDir).isDirectory()) {
      return { attempted: false, sent: false, reason: "run-dir-missing" };
    }
    if (!NOTIFY_ELIGIBLE_MODES.has(mode)) {
      return { attempted: false, sent: false, reason: "mode-not-eligible" };
    }

    const executionContext = getExecutionContext(env);
    const decision = shouldNotify({
      notificationMode,
      executionContext,
      webhookUrl,
    });
    if (!decision.notify) {
      return { attempted: false, sent: false, reason: decision.reason };
    }

    // Operator re-attempt (force overwrite) is PR5 only - not implemented here.
    const markerPath = markerPathFor(runDir);
    const created = createPendingMarker(markerPath);
    if (!created) {
      return { attempted: false, sent: false, reason: "already-attempted" };
    }

    // ASCII body (AGENTS.md); design middle-dot rendered as " / "
    const bodyText = `${mode} ${lifecycle} / ${runId} / ${durationSeconds}s`;
    const effectiveMode = NOTIFY_MODES.has(notificationMode) ? notificationMode : "off";
    let sendResult;

    if (effectiveMode === "webhook") {
      sendResult = await sendWebhook(String(webhookUrl).trim(), {
        runId,
        mode,
        lifecycle,
        durationSeconds,
      });
    } else {
      // auto (background only, already gated) or native → OS notification
      sendResult = sendNative(TITLE, bodyText);
    }

    completeMarker(markerPath, {
      adapter: sendResult.adapter,
      result: sendResult.ok ? "sent" : "failed",
      detail: sendResult.detail,
    });

    return {
      attempted: true,
      sent: Boolean(sendResult.ok),
      reason: sendResult.ok ? "sent" : "send-failed",
      detail: sendResult.detail,
    };
  } catch (err) {
    // Never fail the job on notify errors
    try {
      if (opts.runDir) {
        const markerPath = markerPathFor(opts.runDir);
        if (fs.existsSync(markerPath)) {
          completeMarker(markerPath, {
            adapter: null,
            result: "failed",
            detail: err instanceof Error ? err.message : String(err),
          });
        }
      }
    } catch {
      /* swallow */
    }
    return {
      attempted: true,
      sent: false,
      reason: "notify-exception",
      detail: err instanceof Error ? err.message : String(err),
    };
  }
}
