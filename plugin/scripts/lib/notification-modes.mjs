// plugin/scripts/lib/notification-modes.mjs
//
// Single product source for completion-notification mode strings and webhook URL
// shape. Imported by jobs.mjs (prefs), notify.mjs (adapters), and the companion
// (setup). Keep this module free of jobs-index / state-root I/O.

/** Product set of notification mode strings. */
export const NOTIFICATION_MODES = Object.freeze(["off", "auto", "native", "webhook"]);

const NOTIFICATION_MODE_SET = new Set(NOTIFICATION_MODES);

/**
 * @param {unknown} value
 * @returns {boolean}
 */
export function isNotificationMode(value) {
  return typeof value === "string" && NOTIFICATION_MODE_SET.has(value.trim().toLowerCase());
}

/**
 * @param {unknown} value
 * @returns {string|null} lowercased mode, or null if invalid
 */
export function parseNotificationMode(value) {
  if (typeof value !== "string") {
    return null;
  }
  const mode = value.trim().toLowerCase();
  return NOTIFICATION_MODE_SET.has(mode) ? mode : null;
}

/**
 * Parse a webhook URL for storage. Empty clears. Non-http(s) is invalid.
 *
 * @param {unknown} value
 * @returns {{ ok: true, url: string|null } | { ok: false, reason: string }}
 */
export function parseWebhookUrl(value) {
  if (value == null || value === "") {
    return { ok: true, url: null };
  }
  if (typeof value !== "string") {
    return { ok: false, reason: "webhook-url-not-string" };
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return { ok: true, url: null };
  }
  let url;
  try {
    url = new URL(trimmed);
  } catch {
    return { ok: false, reason: "webhook-url-invalid" };
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return { ok: false, reason: "webhook-protocol-not-http(s)" };
  }
  return { ok: true, url: trimmed };
}

/**
 * Whether the companion should attempt a terminal notify (debate intermediate
 * rounds and --no-notify suppress this).
 *
 * @param {{ skipNotify?: boolean }} [opts]
 * @returns {boolean}
 */
export function shouldAttemptTerminalNotify(opts = {}) {
  return opts.skipNotify !== true;
}

/**
 * Setup-report display for a stored webhook URL: scheme + host only.
 * Never include userinfo, path, query, or fragment (Slack/Discord put secrets in the path).
 *
 * @param {string|null|undefined} urlString
 * @returns {string} e.g. "https://hooks.example.com" or "(set)" or "none"
 */
export function formatWebhookDisplay(urlString) {
  if (urlString == null || urlString === "") {
    return "none";
  }
  if (typeof urlString !== "string") {
    return "(set)";
  }
  try {
    const u = new URL(urlString.trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      return "(set)";
    }
    return `${u.protocol}//${u.host}`;
  } catch {
    return "(set)";
  }
}
