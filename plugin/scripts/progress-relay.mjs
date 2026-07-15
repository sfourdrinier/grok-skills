// plugin/scripts/progress-relay.mjs
//
// T2-2 progress relay (decision D-STREAM): surface a Grok run's C3 progress
// stream (rich thought/text/tool activity from T2-0) as human-visible progress
// lines. This module is PURE plugin-side surfacing: it reads the run's
// progress.jsonl and formats events to a caller-provided sink (stderr in the
// shim). It NEVER writes stdout, NEVER changes/loses/duplicates the wrapper's
// envelope, and NEVER touches any safety boundary - the hardened Python wrapper
// (grok_agent.py) is unchanged.
//
// It mirrors, in Node stdlib only, two facts the wrapper owns:
//   - the run state layout (state_root()/runs/<run_id>/progress.jsonl), from
//     groklib.runstate.state_root / _run_paths_for; and
//   - the tolerant JSONL reader (groklib.progress.read_events): raw bytes split
//     on "\n", blank/torn/invalid lines skipped with a warning, never raising -
//     so a concurrent in-flight append (a partial trailing line) is never
//     surfaced as a broken event.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

// Cross-language mirror of the Python wrapper's runstate constants (the source
// of truth is groklib/runstate.py: _STATE_DIR_NAME and _RUN_ID_PATTERN). They
// are hand-mirrored here because the relay is Node stdlib only and cannot import
// the Python module; the drift-guard test (progress-relay.test.mjs) reads the
// Python constants and asserts these still match, so a divergence is caught.
export const STATE_DIR_NAME = "grok-skills";

// Mirrors runstate._RUN_ID_PATTERN exactly (UTC stamp + 6 hex).
export const RUN_ID_RE = /^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$/;

// F-RELAY-RUNID: the stable prefix the wrapper prints on stderr
// (runstate.RUN_ID_STDERR_MARKER) so the relay can bind to the EXACT run this
// launch created instead of dir-diffing. Mirrored here for the same reason as
// STATE_DIR_NAME / RUN_ID_RE, and covered by the same drift-guard test.
export const RUN_ID_MARKER_PREFIX = "[grok-run-id]";

/**
 * Parse a wrapper stderr line for the run-id handoff marker, returning the run
 * id only when the line is exactly `[grok-run-id] <valid-run-id>` (else null).
 *
 * @param {string} line
 * @returns {string | null}
 */
export function parseRunIdMarker(line) {
  if (typeof line !== "string") {
    return null;
  }
  const trimmed = line.trim();
  if (!trimmed.startsWith(`${RUN_ID_MARKER_PREFIX} `)) {
    return null;
  }
  const candidate = trimmed.slice(RUN_ID_MARKER_PREFIX.length + 1).trim();
  return RUN_ID_RE.test(candidate) ? candidate : null;
}

const DEFAULT_POLL_MS = 150;
const PREVIEW_MAX_CHARS = 200;
// F-RELAY-RACE (round3): the wrapper announces its authoritative run id on stderr
// (RUN_ID_MARKER_PREFIX) within a few hundred ms of creating its run dir. Under
// the single user-global runs/ root a concurrent session's fresh dir can be the
// only unknown candidate in that early window, so the dir-diff FALLBACK must not
// latch a single fresh candidate immediately -- it could belong to another
// session. LiveRelay therefore withholds the dir-diff fallback until this grace
// has elapsed since spawn, giving the authoritative marker time to arrive and
// win; the marker path (adoptRunId) is never gated by this grace.
const DEFAULT_DISCOVERY_GRACE_MS = 1500;

// F-RELAY-SECRET: progress.jsonl carries Grok's RAW thought/text tokens, which
// can quote secret-shaped material Grok read from repo files (an .env value, a
// bearer token, an API key, a JWT). The Python status path already redacts this
// content (groklib/envelope.redact_secret_material) before surfacing it, because
// the fail-closed scanner assert_no_secret_material governs that channel. This
// relay surfaces the SAME raw events to the user's terminal (stderr), so it MUST
// apply the SAME redaction before printing.
//
// The pattern SOURCES below are a hand-mirror of the Python source of truth
// (groklib/envelope.py _SECRET_VALUE_PATTERNS). Node stdlib cannot import the
// Python module, so the drift-guard test (progress-relay.test.mjs) reads the
// Python table and asserts these byte-identical strings still match, so the two
// redactors cannot diverge. Each entry is [label, pythonPatternSource]; the JS
// RegExp is compiled mechanically from that exact source (only the leading Python
// inline "(?i)" flag is translated to the JS "i" flag), so updating the mirrored
// source updates behavior in lock-step.
export const SECRET_VALUE_PATTERNS_PY = [
  ["bearer-token", "(?i)\\bbearer\\s+(?=[A-Za-z0-9._~+/=-]*[0-9._~+/=]|[A-Za-z0-9._~+/=-]{20,})[A-Za-z0-9._~+/=-]{6,}"],
  [
    "api-key-token",
    "\\b(?:xai-(?=[A-Za-z0-9_]*[0-9])[A-Za-z0-9_]{20,}|sk-proj-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{40,}|sk-ant-[a-z0-9]+-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{40,}|sk-(?=[A-Za-z0-9_]*[0-9])[A-Za-z0-9_]{20,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,})",
  ],
  ["jwt", "\\beyJ[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{6,}\\.[A-Za-z0-9_-]{6,}"],
  ["aws-access-key-id", "\\b(?:AKIA|ASIA)[0-9A-Z]{16}\\b"],
  ["github-token", "\\b(?:gh[posru]_|github_pat_)[A-Za-z0-9_]{20,}"],
  ["slack-token", "\\bxox[baprs]-[A-Za-z0-9-]+"],
  ["slack-webhook", "https://hooks\\.slack\\.com/services/[A-Za-z0-9/_-]+"],
  ["pem-private-key", "-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\\s\\S]*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|$)"],
];

/**
 * Compile one mirrored Python pattern source into a global JS RegExp. The only
 * Python-specific construct in the table is a leading inline `(?i)` flag, which
 * JS does not support inline; it is translated to the `i` RegExp flag. Every
 * other construct (\b, \s, character classes, lookahead, quantifiers) is
 * identical between Python and JS regex syntax.
 *
 * @param {string} pySource
 * @returns {RegExp}
 */
function compileSecretPattern(pySource) {
  let body = pySource;
  let flags = "g";
  if (body.startsWith("(?i)")) {
    body = body.slice("(?i)".length);
    flags += "i";
  }
  return new RegExp(body, flags);
}

const SECRET_VALUE_REDACTORS = SECRET_VALUE_PATTERNS_PY.map(([label, pySource]) => ({
  label,
  regex: compileSecretPattern(pySource),
}));

/**
 * Replace every secret-shaped substring in `text` with a labeled placeholder,
 * mirroring envelope.redact_secret_value_text: each pattern is applied in turn
 * and each match becomes `[redacted-<label>]`, so the whole credential span
 * (label plus body) is removed and the placeholders themselves match no pattern.
 * Non-strings pass through unchanged.
 *
 * @param {string} text
 * @returns {string}
 */
export function redactSecretText(text) {
  if (typeof text !== "string" || text === "") {
    return text;
  }
  let redacted = text;
  for (const { label, regex } of SECRET_VALUE_REDACTORS) {
    regex.lastIndex = 0;
    redacted = redacted.replace(regex, `[redacted-${label}]`);
  }
  return redacted;
}

/**
 * F-RELAY-TERMINAL-ESCAPE (PR968 codex terminal-escape): strip control and
 * escape-introducing bytes from model-streamed text before it is written to the
 * terminal. Grok's raw thought/answer tokens (and any repo content it echoes)
 * can carry ANSI/OSC control sequences; sinking them verbatim would turn
 * model-controlled bytes into terminal control input (cursor rewrites, OSC-52
 * clipboard writes, screen clears, etc.). Removing every C0 control (0x00-0x1F,
 * which includes ESC 0x1B and BEL 0x07), DEL (0x7F), and C1 control (0x80-0x9F,
 * including the 8-bit CSI/OSC introducers) neutralizes the introducers, so any
 * residual bytes (e.g. "[31m", "]52;...") are inert printable text. Operating on
 * the JS string means only the actual C1 control CODE POINTS are removed;
 * printable Unicode (accents, emoji, CJK) is preserved. Applied AFTER redaction
 * so the existing secret placeholders (all printable) are kept intact.
 *
 * @param {string} text
 * @returns {string}
 */
export function neutralizeControlSequences(text) {
  if (typeof text !== "string" || text === "") {
    return text;
  }
  // eslint-disable-next-line no-control-regex
  return text.replace(/[\u0000-\u001F\u007F-\u009F]/g, "");
}

// Mirrors envelope._REDACTED_STREAM_PLACEHOLDER: a single placeholder each
// contiguous masked run collapses to across the joined stream.
const REDACTED_STREAM_PLACEHOLDER = "[redacted-secret]";

/**
 * Rebuild `text[start:end)`, collapsing each maximal masked run into one
 * placeholder. Mirrors envelope._apply_mask_slice.
 *
 * @param {string} text
 * @param {Uint8Array} masked
 * @param {number} start
 * @param {number} end
 * @returns {string}
 */
function applyMaskSlice(text, masked, start, end) {
  const pieces = [];
  let index = start;
  while (index < end) {
    if (masked[index]) {
      pieces.push(REDACTED_STREAM_PLACEHOLDER);
      while (index < end && masked[index]) {
        index += 1;
      }
    } else {
      const runStart = index;
      while (index < end && !masked[index]) {
        index += 1;
      }
      pieces.push(text.slice(runStart, index));
    }
  }
  return pieces.join("");
}

/**
 * F1-relay-cross-boundary: redact secret spans across the CONCATENATION of
 * `segments` (a chunked text stream), mirroring envelope.redact_secret_text_stream.
 * The segments are treated as ONE continuous text, so a secret split across a
 * segment boundary is masked in both halves even though neither half matches on
 * its own -- the stream-boundary redaction the per-event redactor lacked. Returns
 * a NEW array of the same length; each returned segment is its slice of the
 * redacted concatenation. Shares SECRET_VALUE_REDACTORS with the per-value
 * redactor, so the two can never diverge.
 *
 * @param {string[]} segments
 * @returns {string[]}
 */
export function redactSecretTextStream(segments) {
  const joined = segments.join("");
  if (!joined) {
    return [...segments];
  }
  const masked = new Uint8Array(joined.length);
  for (const { regex } of SECRET_VALUE_REDACTORS) {
    regex.lastIndex = 0;
    let match;
    while ((match = regex.exec(joined)) !== null) {
      for (let i = match.index; i < match.index + match[0].length; i += 1) {
        masked[i] = 1;
      }
      // A zero-width match would otherwise loop forever; advance past it.
      if (match[0].length === 0) {
        regex.lastIndex += 1;
      }
    }
  }
  let anyMasked = false;
  for (let i = 0; i < masked.length; i += 1) {
    if (masked[i]) {
      anyMasked = true;
      break;
    }
  }
  if (!anyMasked) {
    return [...segments];
  }
  const result = [];
  let cursor = 0;
  for (const segment of segments) {
    const segmentEnd = cursor + segment.length;
    result.push(applyMaskSlice(joined, masked, cursor, segmentEnd));
    cursor = segmentEnd;
  }
  return result;
}

/**
 * Extract an event's `data.text` string, or null when it is absent/non-string.
 *
 * @param {object} event
 * @returns {string | null}
 */
function eventDataText(event) {
  if (!event || typeof event !== "object") {
    return null;
  }
  const data = event.data;
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    return null;
  }
  return typeof data.text === "string" ? data.text : null;
}

/**
 * Format a batch of events to lines, applying CROSS-EVENT stream redaction to
 * their `data.text` values first (F1-relay-cross-boundary), mirroring the Python
 * status path (status._stream_redact_event_text). A secret split across two
 * consecutive events is masked in both halves; each event's line is then formatted
 * from its already-stream-redacted text. Events without a string `data.text` are
 * formatted unchanged.
 *
 * @param {object[]} events
 * @returns {string[]}
 */
export function formatProgressLines(events) {
  const texts = events.map(eventDataText);
  const streamable = texts.filter((value) => value !== null);
  const redactedByIndex = new Map();
  if (streamable.length > 0) {
    const redacted = redactSecretTextStream(streamable);
    let cursor = 0;
    for (let index = 0; index < texts.length; index += 1) {
      if (texts[index] !== null) {
        redactedByIndex.set(index, redacted[cursor]);
        cursor += 1;
      }
    }
  }
  return events.map((event, index) =>
    formatProgressLine(event, redactedByIndex.has(index) ? redactedByIndex.get(index) : undefined)
  );
}

/**
 * Resolve the runs directory the wrapper writes under, mirroring
 * runstate.state_root(): $XDG_STATE_HOME/grok-skills, else
 * ~/.local/state/grok-skills, then /runs.
 *
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function runsDirFor(env = process.env) {
  // Mirror runstate.state_root() EXACTLY, including F-STATE-ABS: honor
  // XDG_STATE_HOME only when it is a non-empty ABSOLUTE path (the XDG spec
  // ignores a relative value, and the Python side falls back to the default for
  // it); otherwise the relay would resolve a different runs dir than the wrapper
  // wrote to and surface nothing.
  const xdg = (env.XDG_STATE_HOME ?? "").trim();
  const base = xdg && path.isAbsolute(xdg) ? xdg : path.join(os.homedir(), ".local", "state");
  return path.join(base, STATE_DIR_NAME, "runs");
}

/**
 * Return the progress.jsonl path for a run id under a runs dir. The caller is
 * responsible for having validated the run id shape when it came from outside.
 *
 * @param {string} runsDir
 * @param {string} runId
 * @returns {string}
 */
export function progressPathFor(runsDir, runId) {
  return path.join(runsDir, runId, "progress.jsonl");
}

/**
 * Snapshot the set of existing valid run-id directory names. Best-effort: a
 * missing or unreadable runs dir yields an empty set (the relay then simply
 * waits for the dir to appear), never a throw.
 *
 * @param {string} runsDir
 * @returns {Set<string>}
 */
export function snapshotRunIds(runsDir) {
  const found = new Set();
  let entries;
  try {
    entries = fs.readdirSync(runsDir, { withFileTypes: true });
  } catch (err) {
    // Missing dir before the first run is normal; anything else is logged.
    if (err && err.code !== "ENOENT") {
      process.stderr.write(`[grok-relay] snapshotRunIds: cannot read ${runsDir}: ${err.message}\n`);
    }
    return found;
  }
  for (const entry of entries) {
    if (entry.isDirectory() && RUN_ID_RE.test(entry.name)) {
      found.add(entry.name);
    }
  }
  return found;
}

/**
 * Discover the run dir this launch created: a valid run id NOT in knownRunIds
 * whose directory ctime/mtime is at/after startMs (minus a small clock skew).
 *
 * F-RELAY-RACE: this dir-diff is only a FALLBACK used until the wrapper's
 * authoritative stderr run-id marker arrives (LiveRelay prefers `_adoptedRunId`).
 * Under the single user-global runs/ root, two sessions launching close together
 * share it, so a naive "newest id wins" guess can transiently latch a DIFFERENT
 * concurrent run's progress and print it to this session's terminal. To stay
 * conservative: (1) ignore any dir created before this spawn (a pre-existing
 * decoy, within a small clock skew), and (2) when more than one fresh candidate
 * qualifies, DO NOT guess -- return null and wait for the authoritative marker
 * to correlate the exact run. Returns the run id, or null when none has appeared
 * yet or the choice is ambiguous. Never throws.
 *
 * @param {string} runsDir
 * @param {Set<string>} knownRunIds
 * @param {number} startMs
 * @returns {string | null}
 */
export function discoverNewRunDir(runsDir, knownRunIds, startMs) {
  let entries;
  try {
    entries = fs.readdirSync(runsDir, { withFileTypes: true });
  } catch (err) {
    if (err && err.code !== "ENOENT") {
      process.stderr.write(`[grok-relay] discoverNewRunDir: cannot read ${runsDir}: ${err.message}\n`);
    }
    return null;
  }

  const skewMs = 2000;
  const candidates = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !RUN_ID_RE.test(entry.name)) {
      continue;
    }
    if (knownRunIds.has(entry.name)) {
      continue;
    }
    let stat;
    try {
      stat = fs.statSync(path.join(runsDir, entry.name));
    } catch (err) {
      process.stderr.write(`[grok-relay] discoverNewRunDir: cannot stat ${entry.name}: ${err.message}\n`);
      continue;
    }
    const createdMs = Math.max(stat.ctimeMs, stat.mtimeMs);
    if (createdMs + skewMs < startMs) {
      // A dir created before this spawn (a stale or concurrent decoy); not this
      // launch. Never latch a run whose creation predates our snapshot.
      continue;
    }
    candidates.push(entry.name);
  }

  if (candidates.length !== 1) {
    // Zero: none has appeared yet. More than one: ambiguous (parallel sessions
    // under the shared runs/ root) -- refuse to guess and wait for the wrapper's
    // authoritative run-id marker rather than risk latching another run.
    return null;
  }
  return candidates[0];
}

/**
 * Tolerant C3 reader, mirroring groklib.progress.read_events. Reads raw bytes,
 * splits on "\n", skips blank lines, and skips any line that is not a JSON
 * object (a torn trailing append, non-JSON, or non-object) with a warning
 * string. A missing file returns an empty event list plus one warning. Never
 * throws for content or a missing/unreadable file.
 *
 * @param {string} filePath
 * @returns {{ events: object[], warnings: string[] }}
 */
export function readProgressEvents(filePath) {
  const events = [];
  const warnings = [];

  let raw;
  try {
    raw = fs.readFileSync(filePath);
  } catch (err) {
    if (err && err.code === "ENOENT") {
      warnings.push(`progress stream not found: ${filePath}`);
      return { events, warnings };
    }
    process.stderr.write(`[grok-relay] readProgressEvents: cannot read ${filePath}: ${err.message}\n`);
    warnings.push(`failed to read progress stream ${filePath}: ${err.message}`);
    return { events, warnings };
  }

  const lines = raw.toString("utf8").split("\n");
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim()) {
      continue;
    }
    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch (err) {
      warnings.push(`skipped invalid JSON at line ${index + 1} of ${filePath}: ${err.message}`);
      continue;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      warnings.push(`skipped non-object JSON at line ${index + 1} of ${filePath}`);
      continue;
    }
    events.push(parsed);
  }

  return { events, warnings };
}

/**
 * Format one C3 event as a single concise human-readable progress line (no
 * trailing newline). Tolerant of missing/wrong-typed fields. For the T2-0
 * coalesced token events (data.text present) a whitespace-collapsed, clipped
 * preview of the streamed thought/answer text is appended, which is the live
 * activity the relay exists to surface.
 *
 * `redactedTextOverride`, when provided, is the event's `data.text` ALREADY
 * stream-redacted across event boundaries (formatProgressLines passes it so a
 * secret split across two events is masked in both halves); when omitted, the
 * event's own `data.text` is per-value redacted as a standalone fallback.
 *
 * @param {object} event
 * @param {string} [redactedTextOverride]
 * @returns {string}
 */
export function formatProgressLine(event, redactedTextOverride) {
  const phase = typeof event.phase === "string" ? event.phase : "?";
  const level = typeof event.level === "string" ? event.level : "info";
  // F-RELAY-SECRET: redact before printing. The message is a wrapper template
  // today, but the data.text preview is Grok's raw streamed content, which can
  // carry secret-shaped material; both are scrubbed with the SAME patterns the
  // Python status path uses, so nothing secret-shaped reaches the terminal.
  // Redact secret-shaped spans, THEN neutralize terminal control/escape
  // sequences (PR968 codex terminal-escape) so model-controlled bytes cannot
  // drive the terminal; redaction runs first so its placeholders survive.
  const message = neutralizeControlSequences(
    redactSecretText(typeof event.message === "string" ? event.message : "")
  );
  const tag = level === "error" ? "ERROR " : level === "warning" ? "WARN " : "";

  let line = `[grok] ${tag}${phase}: ${message}`;

  const data = event.data;
  if (data !== null && typeof data === "object" && !Array.isArray(data)) {
    // Prefer the cross-event stream-redacted text (F1-relay-cross-boundary) when
    // the batch formatter supplied it; else redact this event's FULL raw text
    // standalone before collapsing/clipping, so a secret is never split by the
    // preview truncation and left partially visible.
    const text =
      redactedTextOverride !== undefined
        ? redactedTextOverride
        : redactSecretText(typeof data.text === "string" ? data.text : "");
    if (text) {
      // Collapse whitespace FIRST (so legitimate newlines/tabs become single
      // spaces, not glued words), THEN neutralize the remaining control/escape
      // bytes (PR968 codex terminal-escape) so ESC/BEL/OSC/CSI/C1 sequences in
      // model-streamed text cannot drive the terminal. Redaction already ran, so
      // its placeholders are preserved.
      const collapsed = neutralizeControlSequences(text.replace(/\s+/g, " ").trim());
      const clipped =
        collapsed.length > PREVIEW_MAX_CHARS ? `${collapsed.slice(0, PREVIEW_MAX_CHARS)} ...` : collapsed;
      if (clipped) {
        line += `  ${clipped}`;
      }
    }
  }

  return line;
}

/**
 * Follows one progress.jsonl file incrementally. Each drain re-reads the whole
 * (small, coalesced) file with the tolerant reader and emits only events past
 * the count already emitted, formatted, to the sink. Index-based dedup is safe
 * because the file is strictly append-only and a torn trailing line is skipped
 * by the reader until it completes, so the completed-event count only grows and
 * never reorders. Reader warnings are intentionally NOT surfaced during live
 * follow (the torn trailing line is an expected, transient append race).
 */
export class ProgressFollower {
  /**
   * @param {{ filePath: string, sink: (line: string) => void }} options
   */
  constructor({ filePath, sink }) {
    this.filePath = filePath;
    this.sink = sink;
    this._emittedCount = 0;
  }

  get emittedCount() {
    return this._emittedCount;
  }

  /**
   * Emit any not-yet-emitted events; return how many were emitted this call.
   *
   * TAIL-HOLDBACK (F1/F4-relay-stream-split): a live poll withholds the NEWEST
   * event, because it may be the FIRST HALF of a secret a later event completes --
   * emitting it now would sink the unredacted prefix to the terminal, which cannot
   * be un-printed. Holding it one drain lets the next drain's cross-event stream
   * redaction (which then sees the completing half) mask it before it is emitted.
   * The FINAL drain (`flush: true`, called by LiveRelay.stop after the run ends)
   * emits the held event regardless -- a secret split across that last boundary
   * with no following event is the documented residual. renderRunProgress (the
   * one-shot status render) is unaffected: it formats the whole history at once.
   *
   * @param {{ flush?: boolean }} [options]
   * @returns {number}
   */
  drain({ flush = false } = {}) {
    const { events } = readProgressEvents(this.filePath);
    const emitUpTo = flush ? events.length : Math.max(events.length - 1, 0);
    if (emitUpTo <= this._emittedCount) {
      return 0;
    }
    // F1-relay-cross-boundary: format over the FULL event history so cross-event
    // stream redaction sees a secret straddling the last-emitted / new-event
    // boundary; only the not-yet-emitted lines (up to the holdback point) are sunk.
    const lines = formatProgressLines(events);
    let emitted = 0;
    for (let index = this._emittedCount; index < emitUpTo; index += 1) {
      this.sink(lines[index]);
      emitted += 1;
    }
    this._emittedCount = emitUpTo;
    return emitted;
  }
}

/**
 * Live foreground relay: polls runs/ for the freshly created run dir, then
 * tail-follows its progress.jsonl to the sink until stopped. Strictly
 * best-effort - any tick failure is logged and disables the relay without ever
 * throwing, so the wrapper subprocess and its envelope are never affected
 * (degrade timings i and ii).
 */
export class LiveRelay {
  /**
   * @param {{
   *   runsDir: string,
   *   knownRunIds: Set<string>,
   *   startMs: number,
   *   sink: (line: string) => void,
   *   pollMs?: number,
   * }} options
   */
  constructor({
    runsDir,
    knownRunIds,
    startMs,
    sink,
    pollMs = DEFAULT_POLL_MS,
    discoveryGraceMs = DEFAULT_DISCOVERY_GRACE_MS,
  }) {
    this.runsDir = runsDir;
    this.knownRunIds = knownRunIds;
    this.startMs = startMs;
    this.sink = sink;
    this.pollMs = pollMs;
    this.discoveryGraceMs = discoveryGraceMs;
    this._timer = null;
    this._follower = null;
    this._disabled = false;
    this.runId = null;
    // dirdiff-single-candidate-cross-session-latch: the sole fresh dir-diff
    // candidate seen on the PREVIOUS poll. A single candidate is latched only after
    // it persists as the sole fresh candidate across two consecutive polls, so a
    // foreign run dir that briefly precedes THIS session's own dir is never adopted.
    this._pendingDirDiffCandidate = null;
    // F-RELAY-RUNID: when the wrapper's stderr run-id marker is parsed, the
    // companion hands it here. An adopted id is AUTHORITATIVE: it is used
    // directly (no dir-diff), and it re-points the follower if a dir-diff guess
    // had already latched onto a different run.
    this._adoptedRunId = null;
  }

  /**
   * Bind the relay to the exact run id the wrapper announced on stderr. Wins
   * over the dir-diff heuristic: if a follower already latched onto a different
   * run, it is discarded and re-created for the adopted run on the next tick.
   * A no-op when the id is already the followed run or is malformed.
   *
   * @param {string} runId
   */
  adoptRunId(runId) {
    if (typeof runId !== "string" || !RUN_ID_RE.test(runId)) {
      return;
    }
    if (this._adoptedRunId === runId && this.runId === runId) {
      return;
    }
    this._adoptedRunId = runId;
    if (this.runId !== runId) {
      // Re-point to the authoritative run; the next tick rebuilds the follower.
      this.runId = null;
      this._follower = null;
    }
  }

  /** Begin polling. Idempotent; a disabled relay does not restart. */
  start() {
    if (this._timer !== null || this._disabled) {
      return;
    }
    this._timer = setInterval(() => this._tick(false), this.pollMs);
    // Do not keep the event loop alive on the timer alone; the child process
    // lifecycle owns termination.
    if (typeof this._timer.unref === "function") {
      this._timer.unref();
    }
  }

  /**
   * One poll cycle. `flush` (the final drain from stop) both emits the tail-held
   * event and lets a single fresh dir-diff candidate latch immediately -- by then
   * the wrapper has exited and its own run dir exists, so a lone remaining candidate
   * is provably ours (a foreign one too would make discoverNewRunDir ambiguous).
   *
   * @param {boolean} flush
   */
  _tick(flush = false) {
    if (this._disabled) {
      return;
    }
    try {
      if (this._follower === null) {
        // Prefer the authoritative wrapper-announced run id (F-RELAY-RUNID). Fall
        // back to the dir-diff heuristic only after the discovery grace has
        // elapsed (F-RELAY-RACE round3): before then a single fresh candidate may
        // belong to a concurrent session, so withhold the guess and wait for the
        // marker rather than latch a foreign run's progress into this terminal.
        let runId = this._adoptedRunId;
        if (runId === null || runId === undefined) {
          if (Date.now() - this.startMs < this.discoveryGraceMs) {
            return;
          }
          const candidate = discoverNewRunDir(this.runsDir, this.knownRunIds, this.startMs);
          if (candidate === null || candidate === undefined) {
            this._pendingDirDiffCandidate = null;
            return;
          }
          // dirdiff-single-candidate-cross-session-latch: a single fresh candidate
          // may be a CONCURRENT session's run dir that appeared before THIS
          // session's own wrapper created its dir (interpreter startup jitter). On a
          // regular poll, require the SAME sole candidate on two consecutive ticks
          // before latching: if our own dir appears in between, discoverNewRunDir
          // sees two fresh candidates and returns null (ambiguous), so the foreign
          // dir is never adopted. The final flush drain bypasses this (our dir
          // exists by then). The authoritative marker path is never gated here.
          if (!flush && this._pendingDirDiffCandidate !== candidate) {
            this._pendingDirDiffCandidate = candidate;
            return;
          }
          runId = candidate;
        }
        if (runId === null || runId === undefined) {
          return;
        }
        this.runId = runId;
        this._follower = new ProgressFollower({
          filePath: progressPathFor(this.runsDir, runId),
          sink: this.sink,
        });
      }
      this._follower.drain({ flush });
    } catch (err) {
      // Degrade (ii): a mid-run relay failure must not touch the run. Disable
      // and log; the wrapper keeps going and its envelope still flows.
      this._disabled = true;
      process.stderr.write(`[grok-relay] disabling live relay after error: ${err.message}\n`);
    }
  }

  /** Stop polling and do a final best-effort FLUSH drain of any remaining events. */
  stop() {
    if (this._timer !== null) {
      clearInterval(this._timer);
      this._timer = null;
    }
    if (this._disabled) {
      return;
    }
    try {
      this._tick(true);
    } catch (err) {
      process.stderr.write(`[grok-relay] final drain error: ${err.message}\n`);
    }
  }
}

/**
 * Parse a `--run-id <id>` (or `--run-id=<id>`) argument, returning the value
 * only when it is a well-formed run id, else null. Used by the status path to
 * find the deterministic progress path without touching stdout.
 *
 * PR968 codex status-runid-match: the Python wrapper parses `--run-id` with
 * argparse's default store action, so a DUPLICATED `--run-id A --run-id B`
 * resolves to the LAST value (B) -- and the wrapper validates and authors its
 * envelope for B. This parser MUST mirror that last-value semantics, otherwise
 * the companion would render an EARLIER run's progress (A) that the wrapper's
 * ownership check never validated. We therefore scan every occurrence and keep
 * the last, validating only that final value (null when it is malformed, so a
 * bad trailing value never falls back to an earlier run).
 *
 * @param {string[]} args
 * @returns {string | null}
 */
export function parseRunIdArg(args) {
  let lastValue = null;
  let sawRunId = false;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--run-id") {
      sawRunId = true;
      lastValue = args[index + 1];
      index += 1;
      continue;
    }
    if (typeof arg === "string" && arg.startsWith("--run-id=")) {
      sawRunId = true;
      lastValue = arg.slice("--run-id=".length);
    }
  }
  if (!sawRunId) {
    return null;
  }
  if (typeof lastValue === "string" && RUN_ID_RE.test(lastValue)) {
    return lastValue;
  }
  return null;
}

/**
 * Render a completed run's progress feed once (background /grok:status). Reads
 * the deterministic progress path and formats every event to the sink. Any
 * reader warnings are surfaced (unlike live follow) so a truly missing/malformed
 * stream is visible. Best-effort: never throws.
 *
 * @param {{ runsDir: string, runId: string, sink: (line: string) => void }} options
 * @returns {number} number of events rendered
 */
export function renderRunProgress({ runsDir, runId, sink }) {
  try {
    const filePath = progressPathFor(runsDir, runId);
    const { events, warnings } = readProgressEvents(filePath);
    for (const warning of warnings) {
      sink(`[grok] WARN status: ${warning}`);
    }
    // F1-relay-cross-boundary: cross-event stream redaction (mirrors the Python
    // status path) so a secret split across two events is masked in both halves.
    const lines = formatProgressLines(events);
    for (const line of lines) {
      sink(line);
    }
    return events.length;
  } catch (err) {
    process.stderr.write(`[grok-relay] renderRunProgress error: ${err.message}\n`);
    return 0;
  }
}
