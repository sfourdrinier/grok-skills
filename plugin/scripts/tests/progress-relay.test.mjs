// plugin/scripts/tests/progress-relay.test.mjs
//
// Unit tests for the T2-2 progress relay building blocks: the tolerant C3
// reader, the human-readable formatter, incremental follow with torn-line
// tolerance, run-dir discovery, run-id parsing, and the best-effort
// never-throw contract that underpins the degrade-to-Tier-1 state machine.
// Run with: node --test plugin/scripts/tests/

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  LiveRelay,
  ProgressFollower,
  RUN_ID_MARKER_PREFIX,
  RUN_ID_RE,
  SECRET_VALUE_PATTERNS_PY,
  STATE_DIR_NAME,
  discoverNewRunDir,
  formatProgressLine,
  formatProgressLines,
  neutralizeControlSequences,
  parseRunIdArg,
  parseRunIdMarker,
  progressPathFor,
  readProgressEvents,
  redactSecretText,
  redactSecretTextStream,
  renderRunProgress,
  runsDirFor,
  safeRunIdForRunsDir,
  snapshotRunIds,
} from "../progress-relay.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
// AGENTS.md #8: reconstruct secret-shaped fixtures at runtime so source never
// holds a contiguous token scanners treat as live credentials.
const fx = (...parts) => parts.join("");
const SK_LEGACY = fx("sk-", "abcdef0123456789ABCDEFGHIJKLMNOP");
const XAI_LONG = fx("xai-", "abcdefghijklmnopqrstuvwxyz0123456789");
const JWT_SHORT = fx("eyJhbGciOiJIUzI1NiJ9.", "eyJzdWIiOiIxMjMifQ.", "aGVsbG8xMjM");
const BEARER_OPAQUE = fx("4f8a9c3d2e1b7f6a5d4c3b2a1908f7e6d5c4b3a2", "secretvalue");
const GHP = fx("ghp_", "1234567890abcdefghijklmnopqrstuvwx");
const XOXB = fx("xoxb-", "123456789012-123456789012-abcdefABCDEF");
const BEARER_ALPHA = fx("AbCdEfGhIjKlMnOp", "QrStUv");
const SK_PROJ = fx(
  "sk-proj-",
  "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
);
const BEARER_SHORT = fx("abc", "123");
const PEM_RSA_BEGIN = fx("-----BEGIN ", "RSA PRIVATE KEY-----");
const PEM_RSA_END = fx("-----END ", "RSA PRIVATE KEY-----");
const RUNSTATE_PY = path.resolve(
  SCRIPT_DIR,
  "..",
  "..",
  "wrapper",
  "scripts",
  "groklib",
  "runstate.py"
);
const ENVELOPE_PY = path.resolve(path.dirname(RUNSTATE_PY), "redaction.py");

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-relay-test-"));
}

function collector() {
  const lines = [];
  return { sink: (line) => lines.push(line), lines };
}

const VALID_RUN_ID = "20260715T025610Z-dab154";

test("runsDirFor mirrors XDG_STATE_HOME then home/.local/state", () => {
  assert.equal(
    runsDirFor({ XDG_STATE_HOME: "/x/state" }),
    path.join("/x/state", "grok-skills", "runs")
  );
  assert.equal(
    runsDirFor({}),
    path.join(os.homedir(), ".local", "state", "grok-skills", "runs")
  );
});

test("RUN_ID_RE accepts the real shape and rejects near-misses", () => {
  assert.ok(RUN_ID_RE.test(VALID_RUN_ID));
  assert.ok(!RUN_ID_RE.test("20260715T025610Z-DAB154")); // uppercase hex
  assert.ok(!RUN_ID_RE.test("nope"));
  assert.ok(!RUN_ID_RE.test("../etc"));
});

test("safeRunIdForRunsDir accepts valid ids under runsDir and rejects traversal", () => {
  const runsDir = path.join("/tmp", "grok-skills-runs");
  assert.equal(safeRunIdForRunsDir(VALID_RUN_ID, runsDir), VALID_RUN_ID);
  assert.equal(safeRunIdForRunsDir("../etc", runsDir), null);
  assert.equal(safeRunIdForRunsDir("direct-123", runsDir), null);
  assert.equal(safeRunIdForRunsDir(null, runsDir), null);
  assert.equal(safeRunIdForRunsDir(VALID_RUN_ID, ""), null);
  // Shape-valid ids are accepted; path containment is belt-and-suspenders
  // after resolve (RUN_ID_RE already forbids ../ and path separators).
  const resolved = path.resolve(runsDir, VALID_RUN_ID);
  assert.ok(resolved.startsWith(path.resolve(runsDir) + path.sep));
});

test("formatProgressLine renders phase + message and a token-text preview", () => {
  const line = formatProgressLine({
    phase: "grok",
    level: "info",
    message: "grok streamed thought tokens",
    data: { event: "thought", chars: 19, text: "thinking   about\nPONG" },
  });
  assert.match(line, /\[grok\] grok: grok streamed thought tokens/);
  // whitespace is collapsed in the preview
  assert.match(line, /thinking about PONG/);
});

test("formatProgressLine tags warning and error levels", () => {
  assert.match(formatProgressLine({ phase: "validate", level: "warning", message: "hmm" }), /WARN validate: hmm/);
  assert.match(formatProgressLine({ phase: "grok", level: "error", message: "boom" }), /ERROR grok: boom/);
});

test("formatProgressLine tolerates missing/wrong-typed fields without throwing", () => {
  assert.doesNotThrow(() => formatProgressLine({}));
  assert.doesNotThrow(() => formatProgressLine({ phase: 3, message: null, data: "not-an-object" }));
  const line = formatProgressLine({ data: { text: 123 } });
  assert.match(line, /\[grok\] \?:/);
});

test("readProgressEvents skips torn/invalid lines and never throws", () => {
  const dir = tmpDir();
  const file = path.join(dir, "progress.jsonl");
  const good1 = JSON.stringify({ seq: 1, phase: "start", level: "info", message: "a" });
  const good2 = JSON.stringify({ seq: 2, phase: "grok", level: "info", message: "b" });
  // a non-object JSON, a non-JSON line, then a torn trailing partial (no newline)
  fs.writeFileSync(file, `${good1}\n42\nnot-json{\n${good2}\n{"seq":3,"phase":"do`);

  const { events, warnings } = readProgressEvents(file);
  assert.equal(events.length, 2);
  assert.equal(events[0].message, "a");
  assert.equal(events[1].message, "b");
  assert.ok(warnings.length >= 2, "expected warnings for the bad lines");
});

test("readProgressEvents returns a warning (not a throw) for a missing file", () => {
  const { events, warnings } = readProgressEvents(path.join(tmpDir(), "nope.jsonl"));
  assert.equal(events.length, 0);
  assert.match(warnings[0], /not found/);
});

test("ProgressFollower emits each event once, holding the tail event back one drain", () => {
  const dir = tmpDir();
  const file = path.join(dir, "progress.jsonl");
  fs.writeFileSync(file, `${JSON.stringify({ seq: 1, phase: "start", message: "one" })}\n`);

  const { sink, lines } = collector();
  const follower = new ProgressFollower({ filePath: file, sink });

  // TAIL-HOLDBACK: the newest event is withheld until a later event confirms it
  // (or a flush at stop), so a live drain of a single event emits nothing yet.
  assert.equal(follower.drain(), 0, "the sole (newest) event is held back");
  assert.equal(follower.drain(), 0, "still nothing new to confirm the held event");

  // Append a complete line plus a torn trailing partial: now event 1 has a
  // follower, so it is emitted; event 2 becomes the new held tail.
  fs.appendFileSync(file, `${JSON.stringify({ seq: 2, phase: "grok", message: "two" })}\n{"seq":3,"phase":"do`);
  assert.equal(follower.drain(), 1, "the confirmed earlier event is emitted; the new tail is held");
  assert.equal(follower.drain(), 0, "the torn trailing line does not confirm the held tail");

  // Complete the torn line: event 2 is now confirmed by event 3 and emitted.
  fs.appendFileSync(file, `ne","message":"three"}\n`);
  assert.equal(follower.drain(), 1, "event two is emitted once event three confirms it");

  // The final flush (stop) emits the last held event.
  assert.equal(follower.drain({ flush: true }), 1, "flush emits the last held event");

  assert.equal(lines.length, 3);
  assert.match(lines[0], /one/);
  assert.match(lines[1], /two/);
  assert.match(lines[2], /three/);
});

test("F1/F4-relay-stream-split: a secret split across two drain ticks is redacted before reaching the sink", () => {
  const dir = tmpDir();
  const file = path.join(dir, "progress.jsonl");
  // Event 1 ends mid-bearer-token: "Bearer abcdefghij" alone is a 10-char all-alpha
  // body that matches NO pattern (the bearer body needs a digit OR >=20 chars), so
  // WITHOUT the tail-holdback it would print unredacted and could not be un-printed.
  fs.writeFileSync(
    file,
    `${JSON.stringify({ phase: "grok", message: "s", data: { text: "the header is Bearer abcdefghij" } })}\n`
  );
  const { sink, lines } = collector();
  const follower = new ProgressFollower({ filePath: file, sink });

  // First tick: the split-secret prefix is HELD, so nothing leaks to the sink.
  assert.equal(follower.drain(), 0);
  assert.equal(lines.length, 0, "the tail event holding the split-secret prefix is not emitted");

  // The completing half arrives in a later tick; the held event is now emitted with
  // the secret stream-redacted across the joined text.
  fs.appendFileSync(
    file,
    `${JSON.stringify({ phase: "grok", message: "s", data: { text: "klmnopqrstuvwxyz done" } })}\n`
  );
  follower.drain();
  follower.drain({ flush: true });

  const joined = lines.join("\n");
  assert.ok(!joined.includes("abcdefghijklmnopqrstuvwxyz"), "the joined split bearer token must not survive");
  assert.ok(!joined.includes("Bearer abcdefghij"), "the unredacted first half must never reach the sink");
  assert.ok(joined.includes("[redacted-secret]"), "the split secret is stream-redacted");
});

test("snapshotRunIds + discoverNewRunDir find the freshly created run dir", () => {
  const runsDir = tmpDir();
  const existing = "20260101T000000Z-aaaaaa";
  fs.mkdirSync(path.join(runsDir, existing));

  const known = snapshotRunIds(runsDir);
  assert.ok(known.has(existing));

  const startMs = Date.now();
  assert.equal(discoverNewRunDir(runsDir, known, startMs), null, "nothing new yet");

  const fresh = "20260715T030000Z-bbbbbb";
  fs.mkdirSync(path.join(runsDir, fresh));
  // a non-run-id dir must be ignored
  fs.mkdirSync(path.join(runsDir, "garbage"));

  assert.equal(discoverNewRunDir(runsDir, known, startMs), fresh);
});

test("snapshotRunIds returns empty (no throw) when the runs dir is absent", () => {
  const missing = path.join(tmpDir(), "does", "not", "exist");
  assert.deepEqual([...snapshotRunIds(missing)], []);
});

test("parseRunIdArg accepts --run-id <id> and --run-id=<id>, rejects malformed", () => {
  assert.equal(parseRunIdArg(["status", "--run-id", VALID_RUN_ID]), VALID_RUN_ID);
  assert.equal(parseRunIdArg(["status", `--run-id=${VALID_RUN_ID}`]), VALID_RUN_ID);
  assert.equal(parseRunIdArg(["status", "--run-id", "../evil"]), null);
  assert.equal(parseRunIdArg(["status"]), null);
});

test("parseRunIdArg mirrors argparse last-value semantics on duplicated --run-id", () => {
  // PR968 codex status-runid-match: the Python wrapper (argparse store) validates
  // the LAST --run-id, so a success envelope for B must render B's progress, never
  // the earlier A whose ownership the wrapper never checked.
  const runIdA = "20260715T025610Z-aaa111";
  const runIdB = "20260715T025610Z-bbb222";
  assert.equal(parseRunIdArg(["status", "--run-id", runIdA, "--run-id", runIdB]), runIdB);
  assert.equal(parseRunIdArg(["status", `--run-id=${runIdA}`, `--run-id=${runIdB}`]), runIdB);
  assert.equal(parseRunIdArg(["status", "--run-id", runIdA, `--run-id=${runIdB}`]), runIdB);
  // A malformed trailing value never falls back to an earlier valid run.
  assert.equal(parseRunIdArg(["status", "--run-id", runIdA, "--run-id", "../evil"]), null);
});

test("renderRunProgress formats all events of a completed run to the sink", () => {
  const runsDir = tmpDir();
  const runId = VALID_RUN_ID;
  fs.mkdirSync(path.join(runsDir, runId));
  fs.writeFileSync(
    progressPathFor(runsDir, runId),
    `${JSON.stringify({ seq: 1, phase: "start", message: "created" })}\n` +
      `${JSON.stringify({ seq: 2, phase: "grok", message: "grok streamed thought tokens", data: { text: "hi there" } })}\n`
  );

  const { sink, lines } = collector();
  const count = renderRunProgress({ runsDir, runId, sink });
  assert.equal(count, 2);
  assert.match(lines[0], /created/);
  assert.match(lines[1], /hi there/);
});

test("renderRunProgress surfaces a warning (never throws) for a missing run", () => {
  const runsDir = tmpDir();
  const { sink, lines } = collector();
  assert.doesNotThrow(() => renderRunProgress({ runsDir, runId: VALID_RUN_ID, sink }));
  assert.ok(lines.some((line) => /WARN status/.test(line)));
});

test("LiveRelay disables itself (never throws) when the sink throws mid-drain", () => {
  const runsDir = tmpDir();
  const fresh = "20260715T040000Z-cccccc";
  fs.mkdirSync(path.join(runsDir, fresh));
  fs.writeFileSync(
    progressPathFor(runsDir, fresh),
    `${JSON.stringify({ seq: 1, phase: "start", message: "boom" })}\n`
  );

  const relay = new LiveRelay({
    runsDir,
    knownRunIds: new Set(),
    // startMs well in the past so the dir-diff discovery grace has elapsed and
    // the single fresh candidate is discovered on the final drain (this test is
    // about the throwing-sink degrade, not the F-RELAY-RACE grace).
    startMs: Date.now() - 60_000,
    sink: () => {
      throw new Error("sink exploded");
    },
  });

  // stop() performs a final drain; the throwing sink must be swallowed.
  assert.doesNotThrow(() => relay.stop());
  assert.equal(relay.runId, fresh, "discovery still happened before the sink threw");
});

test("parseRunIdMarker extracts a valid run id and rejects everything else", () => {
  assert.equal(parseRunIdMarker(`[grok-run-id] ${VALID_RUN_ID}`), VALID_RUN_ID);
  assert.equal(parseRunIdMarker(`  [grok-run-id] ${VALID_RUN_ID}  `), VALID_RUN_ID);
  assert.equal(parseRunIdMarker(`[grok-run-id] ../evil`), null, "an unsafe id shape is rejected");
  assert.equal(parseRunIdMarker(`[grok-run-id]${VALID_RUN_ID}`), null, "the space separator is required");
  assert.equal(parseRunIdMarker(`[other-marker] ${VALID_RUN_ID}`), null);
  assert.equal(parseRunIdMarker("grok streamed thought tokens"), null);
  assert.equal(parseRunIdMarker(null), null);
});

test("LiveRelay.adoptRunId follows the announced run, not a lexically-newer dir-diff pick", () => {
  const runsDir = tmpDir();
  const realId = "20260715T050000Z-aaaaaa";
  const decoyId = "20260715T060000Z-ffffff"; // lexically NEWER -> naive dir-diff picks this
  fs.mkdirSync(path.join(runsDir, realId));
  fs.mkdirSync(path.join(runsDir, decoyId));
  fs.writeFileSync(
    progressPathFor(runsDir, realId),
    `${JSON.stringify({ seq: 1, phase: "start", message: "REAL run progress" })}\n`
  );
  fs.writeFileSync(
    progressPathFor(runsDir, decoyId),
    `${JSON.stringify({ seq: 1, phase: "start", message: "DECOY run progress" })}\n`
  );

  const startMs = Date.now();
  // F-RELAY-RACE: with two concurrent fresh candidates the conservative dir-diff
  // refuses to guess (returns null) rather than latch the lexically-newer decoy;
  // adoptRunId then binds the authoritative run.
  assert.equal(discoverNewRunDir(runsDir, new Set(), startMs), null, "ambiguous dir-diff refuses to guess");

  const { sink, lines } = collector();
  const relay = new LiveRelay({ runsDir, knownRunIds: new Set(), startMs, sink });
  relay.adoptRunId(realId);
  relay.stop(); // final drain follows the adopted (authoritative) run
  assert.equal(relay.runId, realId);
  assert.ok(lines.some((line) => /REAL run progress/.test(line)), "the real run's progress is surfaced");
  assert.ok(!lines.some((line) => /DECOY run progress/.test(line)), "the decoy run is never followed");
});

test("LiveRelay.adoptRunId is authoritative and re-points away from a stale dir-diff guess", () => {
  const relay = new LiveRelay({ runsDir: "/x", knownRunIds: new Set(), startMs: Date.now(), sink: () => {} });
  // Simulate a dir-diff tick that already latched onto the wrong (newer) run.
  relay.runId = "20260715T060000Z-ffffff";
  relay._follower = { drain: () => 0 };
  relay.adoptRunId("20260715T050000Z-aaaaaa");
  assert.equal(relay.runId, null, "the stale follower is discarded so the next drain rebuilds for the real run");
  assert.equal(relay._follower, null);

  // Malformed ids and the already-followed id are no-ops.
  relay.runId = VALID_RUN_ID;
  relay._follower = { drain: () => 0 };
  relay.adoptRunId("../evil");
  assert.equal(relay.runId, VALID_RUN_ID, "a malformed id is ignored");
  relay.adoptRunId(VALID_RUN_ID);
  assert.equal(relay.runId, VALID_RUN_ID, "adopting the already-followed run is a no-op");
  assert.notEqual(relay._follower, null);
});

test("F-RELAY-RACE: dir-diff ignores a run created before this spawn (stale/decoy)", () => {
  const runsDir = tmpDir();
  const decoy = "20260715T010000Z-dddddd";
  fs.mkdirSync(path.join(runsDir, decoy));
  // A startMs well after the decoy's real creation time means the decoy predates
  // this spawn (beyond the clock-skew tolerance) and must never be latched.
  const startMs = Date.now() + 10_000;
  assert.equal(
    discoverNewRunDir(runsDir, new Set(), startMs),
    null,
    "a run whose creation predates this spawn is never latched"
  );
});

test("F-RELAY-RACE: dir-diff returns null when two fresh runs are ambiguous", () => {
  const runsDir = tmpDir();
  const startMs = Date.now();
  fs.mkdirSync(path.join(runsDir, "20260715T080000Z-aaaaaa"));
  fs.mkdirSync(path.join(runsDir, "20260715T080001Z-bbbbbb"));
  assert.equal(
    discoverNewRunDir(runsDir, new Set(), startMs),
    null,
    "two concurrent fresh candidates are ambiguous; wait for the marker"
  );
});

test("F-RELAY-RACE: LiveRelay does not latch a single foreign fresh run during the discovery grace", () => {
  const runsDir = tmpDir();
  // A single fresh candidate belonging to ANOTHER session -- the only unknown
  // fresh dir. Within the discovery grace the relay must NOT latch it.
  const foreign = "20260715T090000Z-ffffff";
  fs.mkdirSync(path.join(runsDir, foreign));
  fs.writeFileSync(
    progressPathFor(runsDir, foreign),
    `${JSON.stringify({ seq: 1, phase: "start", message: "FOREIGN run progress" })}\n`
  );

  const { sink, lines } = collector();
  const relay = new LiveRelay({
    runsDir,
    knownRunIds: new Set(),
    startMs: Date.now(), // grace not yet elapsed
    sink,
    discoveryGraceMs: 60_000,
  });
  relay._tick();
  assert.equal(relay.runId, null, "the foreign fresh run is not latched during the grace");
  assert.ok(!lines.some((line) => /FOREIGN run progress/.test(line)), "no foreign progress is surfaced");

  // Once the authoritative marker arrives it binds the real run, never the foreign one.
  const realId = "20260715T090500Z-aaaaaa";
  fs.mkdirSync(path.join(runsDir, realId));
  fs.writeFileSync(
    progressPathFor(runsDir, realId),
    `${JSON.stringify({ seq: 1, phase: "start", message: "REAL run progress" })}\n`
  );
  relay.adoptRunId(realId);
  relay.stop();
  assert.equal(relay.runId, realId);
  assert.ok(lines.some((line) => /REAL run progress/.test(line)), "the adopted run is surfaced");
  assert.ok(!lines.some((line) => /FOREIGN run progress/.test(line)), "the foreign run is never surfaced");
});

test("dirdiff-single-candidate-cross-session-latch: a foreign single candidate is not latched before our own dir appears", () => {
  const runsDir = tmpDir();
  // A single fresh candidate belonging to ANOTHER session appears first (this
  // session's own wrapper dir has not been created yet).
  const foreign = "20260715T093000Z-ffffff";
  fs.mkdirSync(path.join(runsDir, foreign));
  fs.writeFileSync(
    progressPathFor(runsDir, foreign),
    `${JSON.stringify({ phase: "start", message: "FOREIGN run progress" })}\n`
  );

  const { sink, lines } = collector();
  const relay = new LiveRelay({
    runsDir,
    knownRunIds: new Set(),
    startMs: Date.now() - 60_000, // grace already elapsed
    sink,
    discoveryGraceMs: 0,
  });

  // First poll: a single fresh candidate is NOT latched on first sighting.
  relay._tick(false);
  assert.equal(relay.runId, null, "a lone fresh candidate is not latched on first sighting");
  assert.ok(!lines.some((line) => /FOREIGN/.test(line)));

  // Our own dir appears before the next poll -> two fresh candidates -> ambiguous.
  const ours = "20260715T093100Z-aaaaaa";
  fs.mkdirSync(path.join(runsDir, ours));
  fs.writeFileSync(
    progressPathFor(runsDir, ours),
    `${JSON.stringify({ phase: "start", message: "OURS run progress" })}\n`
  );
  relay._tick(false);
  assert.equal(relay.runId, null, "with our own dir now present the choice is ambiguous; foreign is never latched");
  assert.ok(!lines.some((line) => /FOREIGN/.test(line)), "the foreign run's progress is never surfaced");
});

test("dirdiff-single-candidate: a stable sole candidate is latched after a second consecutive sighting", () => {
  const runsDir = tmpDir();
  const ours = "20260715T094000Z-aaaaaa";
  fs.mkdirSync(path.join(runsDir, ours));
  fs.writeFileSync(
    progressPathFor(runsDir, ours),
    `${JSON.stringify({ phase: "start", message: "OURS run progress" })}\n`
  );

  const { sink } = collector();
  const relay = new LiveRelay({
    runsDir,
    knownRunIds: new Set(),
    startMs: Date.now() - 60_000,
    sink,
    discoveryGraceMs: 0,
  });

  relay._tick(false);
  assert.equal(relay.runId, null, "not latched on the first sighting (awaiting confirmation)");
  relay._tick(false);
  assert.equal(relay.runId, ours, "latched once the sole candidate is confirmed on a second consecutive poll");
});

test("F-RELAY-SECRET: formatProgressLine redacts secret-shaped preview text", () => {
  const secrets = [
    [`Authorization: Bearer ${BEARER_OPAQUE}`, BEARER_OPAQUE],
    [`api key ${SK_LEGACY} here`, SK_LEGACY],
    [`token ${XAI_LONG} done`, XAI_LONG],
    [`jwt ${JWT_SHORT} tail`, JWT_SHORT],
  ];
  for (const [text, secretValue] of secrets) {
    const line = formatProgressLine({ phase: "grok", level: "info", message: "grok streamed text tokens", data: { text } });
    assert.ok(!line.includes(secretValue), `redacted line must not contain the secret value: ${secretValue.slice(0, 12)}`);
    assert.match(line, /\[redacted-/, "a redaction placeholder is present");
    // The exported redactor removes the value directly too.
    assert.ok(!redactSecretText(text).includes(secretValue), "redactSecretText removes the value");
  }
});

test("F-RELAY-TERMINAL-ESCAPE: neutralizeControlSequences strips control/escape bytes, keeps printable", () => {
  // CSI colour, OSC-52 clipboard write, cursor move, BEL, DEL, and an 8-bit C1
  // CSI introducer are all removed; the surrounding printable text survives.
  const ESC = "\u001b";
  const BEL = "\u0007";
  const DEL = "\u007f";
  const C1CSI = "\u009b";
  const withControls =
    `hello ${ESC}[31mred${ESC}[0m ${ESC}]52;c;bWFsaWNpb3Vz${BEL} ${ESC}[2J${ESC}[H ${C1CSI}6n ${BEL}bell ${DEL}del world`;
  const cleaned = neutralizeControlSequences(withControls);

  assert.ok(!cleaned.includes(ESC), "ESC removed");
  assert.ok(!cleaned.includes(BEL), "BEL removed");
  assert.ok(!cleaned.includes(DEL), "DEL removed");
  assert.ok(!cleaned.includes(C1CSI), "C1 CSI removed");
  // Printable content (including the inert residue of stripped sequences) remains.
  assert.match(cleaned, /hello .*red.*bell.*del world/);
  // Non-strings and empty pass through unchanged.
  assert.equal(neutralizeControlSequences(""), "");
  assert.equal(neutralizeControlSequences(42), 42);
  // Accents/emoji (printable, outside C1) are preserved.
  assert.equal(neutralizeControlSequences("caf\u00e9 \ud83d\ude80"), "caf\u00e9 \ud83d\ude80");
});

test("F-RELAY-TERMINAL-ESCAPE: formatProgressLine neutralizes control sequences before the sink", () => {
  const ESC = "\u001b";
  const BEL = "\u0007";
  // Streamed data.text carrying ANSI/OSC/cursor sequences must reach stderr with
  // every escape introducer stripped, so it cannot drive the terminal.
  const previewLine = formatProgressLine({
    phase: "grok",
    level: "info",
    message: "grok streamed thought tokens",
    data: { text: `${ESC}[31mDANGER${ESC}[0m ${ESC}]52;c;cHduZWQ=${BEL} payload` },
  });
  assert.ok(!previewLine.includes(ESC), "preview text has no ESC");
  assert.ok(!previewLine.includes(BEL), "preview text has no BEL");
  assert.match(previewLine, /DANGER/, "printable preview content survives");

  // The message channel is neutralized too (it is a wrapper template today, but
  // must never sink model-controllable escapes verbatim).
  const messageLine = formatProgressLine({
    phase: "grok",
    level: "info",
    message: `streamed ${ESC}[2Jclear and ${BEL}bell`,
  });
  assert.ok(!messageLine.includes(ESC), "message has no ESC");
  assert.ok(!messageLine.includes(BEL), "message has no BEL");
  assert.match(messageLine, /streamed .*clear and .*bell/);
});

test("F-RELAY-SECRET: extended credential shapes (aws/github/slack/pem/pure-alpha bearer) are redacted", () => {
  // Split AWS/GitHub/Slack shapes so source never holds a contiguous secret
  // literal (GitHub secret scanning flags AWS docs EXAMPLE keys as Temporary
  // Access Key IDs).
  const secrets = [
    fx("AKIA", "IOSFODNN7EXAMPLE"),
    GHP,
    XOXB,
    `${PEM_RSA_BEGIN}\nMIIBkeymaterial1234567890\n${PEM_RSA_END}`,
    `Bearer ${BEARER_ALPHA}`,
  ];
  for (const secret of secrets) {
    const cleaned = redactSecretText(`leaked ${secret} value`);
    const body = secret.startsWith("Bearer ") ? secret.slice("Bearer ".length) : secret;
    assert.ok(!cleaned.includes(body), `redacted output must not contain: ${body.slice(0, 16)}`);
    assert.match(cleaned, /\[redacted-/, "a redaction placeholder is present");
  }
});

test("F-RELAY-SECRET: benign prose mentioning 'bearer' is not mangled", () => {
  const line = formatProgressLine({
    phase: "grok",
    level: "info",
    message: "grok streamed thought tokens",
    data: { text: "the bearer of good news said hello" },
  });
  assert.match(line, /the bearer of good news said hello/);
  assert.ok(!line.includes("[redacted-"), "benign prose is not redacted");
});

test("F-DRY-SECRET-MIRROR: the Node redaction patterns match the Python redaction source of truth", () => {
  const py = fs.readFileSync(ENVELOPE_PY, "utf8");
  // Anchor to the assignment line (not comments that mention the name).
  const blockMatch = /^_SECRET_VALUE_PATTERNS[^\n]*=\s*\(([\s\S]*?)^\)/m.exec(py);
  assert.ok(blockMatch, "could not find _SECRET_VALUE_PATTERNS in redaction.py");

  const entryRe = /\("([a-z0-9-]+)",\s*re\.compile\(r"([^"]+)"\)\)/g;
  const pyEntries = [];
  let match;
  while ((match = entryRe.exec(blockMatch[1])) !== null) {
    pyEntries.push([match[1], match[2]]);
  }
  assert.equal(pyEntries.length, SECRET_VALUE_PATTERNS_PY.length, "pattern count drifted from Python");
  assert.deepEqual(
    pyEntries,
    SECRET_VALUE_PATTERNS_PY,
    "the Node SECRET_VALUE_PATTERNS_PY mirror drifted from redaction.py _SECRET_VALUE_PATTERNS"
  );
});

test("F-DRY-MIRROR: the Node relay constants match the Python runstate source of truth", () => {
  const py = fs.readFileSync(RUNSTATE_PY, "utf8");

  const stateNameMatch = /_STATE_DIR_NAME\s*=\s*"([^"]+)"/.exec(py);
  const runIdMatch = /_RUN_ID_PATTERN\s*=\s*re\.compile\(r"([^"]+)"\)/.exec(py);
  const markerMatch = /RUN_ID_STDERR_MARKER\s*=\s*"([^"]+)"/.exec(py);

  assert.ok(stateNameMatch, "could not find _STATE_DIR_NAME in runstate.py");
  assert.ok(runIdMatch, "could not find _RUN_ID_PATTERN in runstate.py");
  assert.ok(markerMatch, "could not find RUN_ID_STDERR_MARKER in runstate.py");

  assert.equal(STATE_DIR_NAME, stateNameMatch[1], "STATE_DIR_NAME drifted from the Python constant");
  assert.equal(RUN_ID_RE.source, runIdMatch[1], "RUN_ID_RE drifted from the Python _RUN_ID_PATTERN source");
  assert.equal(RUN_ID_MARKER_PREFIX, markerMatch[1], "RUN_ID_MARKER_PREFIX drifted from the Python marker");
});

test("F1-relay-cross-boundary: a PEM key split across two events is redacted in both halves", () => {
  const segments = [
    `${PEM_RSA_BEGIN}\nMIIBkeymaterial`,
    `0123456789ABCDEF\n${PEM_RSA_END}`,
  ];
  const redacted = redactSecretTextStream(segments);
  const joined = redacted.join("");
  assert.ok(!joined.includes("MIIBkeymaterial"), "the split PEM body must not survive");
  assert.ok(!joined.includes("0123456789ABCDEF"), "the second half of the PEM body must not survive");
  assert.ok(joined.includes("[redacted-secret]"), "the masked span collapses to the stream placeholder");
});

test("F1-relay-cross-boundary: formatProgressLines masks a bearer token split across two events", () => {
  const events = [
    {
      phase: "grok",
      message: "streaming",
      data: { text: `Authorization header is Bearer ${BEARER_SHORT}` },
    },
    { phase: "grok", message: "streaming", data: { text: "DEF456ghijklmnopqrstuvwxyz done" } },
  ];
  const lines = formatProgressLines(events);
  const joined = lines.join("\n");
  assert.ok(
    !joined.includes(`${BEARER_SHORT}DEF456ghijklmnopqrstuvwxyz`),
    "the joined bearer token must not survive"
  );
  assert.ok(!joined.includes("DEF456ghijklmnopqrstuvwxyz"), "the token remainder must not print verbatim");
  assert.ok(joined.includes("[redacted-secret]"), "the split secret is stream-redacted");
});

test("F1-relay-cross-boundary: renderRunProgress stream-redacts a split secret", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-relay-split-"));
  try {
    const runId = "20260715T101010Z-abcdef";
    const runDir = path.join(dir, runId);
    fs.mkdirSync(runDir);
    const lines = [
      JSON.stringify({
        phase: "grok",
        message: "s",
        data: { text: `${PEM_RSA_BEGIN}\nMIIBsecretkeypart` },
      }),
      JSON.stringify({
        phase: "grok",
        message: "s",
        data: { text: `9876543210ZZ\n${PEM_RSA_END}` },
      }),
    ];
    fs.writeFileSync(path.join(runDir, "progress.jsonl"), lines.join("\n") + "\n");
    const sunk = [];
    renderRunProgress({ runsDir: dir, runId, sink: (line) => sunk.push(line) });
    const joined = sunk.join("\n");
    assert.ok(!joined.includes("MIIBsecretkeypart"), "the split PEM body must not reach the status render");
    assert.ok(!joined.includes("9876543210ZZ"), "the PEM remainder must not reach the status render");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("F1-relay-cross-boundary: a current OpenAI project key in one event is redacted", () => {
  const key = SK_PROJ;
  const [line] = formatProgressLines([
    { phase: "grok", message: "streaming", data: { text: `leaked ${key} here` } },
  ]);
  assert.ok(!line.includes(key), "the provider key must not survive the relay");
  assert.ok(line.includes("[redacted-secret]"), "the key is stream-redacted");
});
