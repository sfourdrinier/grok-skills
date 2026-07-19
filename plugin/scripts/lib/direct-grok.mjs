// plugin/scripts/lib/direct-grok.mjs
//
// "Use your installed Grok CLI" posture (parity with OpenAI codex-plugin-cc
// using the installed Codex). Spawns the real `grok` binary with the operator
// home/auth. No private-home isolation and no wrapper sandbox verification.
// Emits a lightweight envelope so skills still see JSON on stdout.
// Owns direct-mode handoff refusal copy (Task 1.6). Implement combo lives in
// lib/implement.mjs so this module stays direct-mode only.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { flagValue, resolveWebFlag } from "./companion-args.mjs";
import { extractTask, stageTaskFile } from "./task-file.mjs";

/** Honest refusal when handoff artifacts are requested for a direct-mode run. */
export const DIRECT_NO_HANDOFF_MSG =
  "direct-mode runs have no hardened run state. Job output: result <id>. For verified handoff artifacts, rerun with setup --run-mode hardened.";

/** Direct-mode runId shape (single source; result/cancel resolve via job index). */
export const DIRECT_RUN_ID_RE = /^direct-[0-9]+$/;

export function isDirectRunId(id) {
  return typeof id === "string" && DIRECT_RUN_ID_RE.test(id);
}

/** Raw --run-id value (no hardened-shape filter). Used for direct-id refusal. */
export function rawRunIdFlag(args) {
  if (!Array.isArray(args)) return null;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--run-id" && typeof args[i + 1] === "string") return args[i + 1];
    if (typeof a === "string" && a.startsWith("--run-id=")) return a.slice("--run-id=".length);
  }
  return null;
}

/** First bare positional after argv[0] that looks like a direct run id. */
export function bareDirectRunId(args) {
  if (!Array.isArray(args)) return null;
  for (let i = 1; i < args.length; i++) {
    const a = args[i];
    if (typeof a === "string" && !a.startsWith("-") && isDirectRunId(a)) return a;
  }
  return null;
}

export function writeDirectNoHandoffRefuse() {
  process.stderr.write(`[grok-companion] ${DIRECT_NO_HANDOFF_MSG}\n`);
  return 1;
}

/** True when status/handoff target a direct-* id (refuse before wrapper spawn). */
export function isDirectHandoffRequest(wrapperMode, args) {
  if (wrapperMode !== "status" && wrapperMode !== "handoff") return false;
  return isDirectRunId(rawRunIdFlag(args)) || isDirectRunId(bareDirectRunId(args));
}

/**
 * Resolve the installed Grok CLI binary.
 * Honors GROK_AGENT_BINARY only (parity with the wrapper); no GROK_BINARY alias.
 */
function resolveGrokBinary(env = process.env) {
  const override = (env.GROK_AGENT_BINARY ?? "").trim();
  if (override) {
    return override;
  }
  const home = env.HOME || os.homedir();
  const candidate = path.join(home, ".grok", "bin", "grok");
  if (fs.existsSync(candidate)) {
    return candidate;
  }
  return "grok";
}

function hasFlag(args, name) {
  // Presence of the bare split form only (used for wrapper-only flags like
  // --isolated that have no equals-value payload). Web resolution uses
  // resolveWebFlag (equals-aware, last-wins with --no-web).
  return Array.isArray(args) && args.includes(name);
}

// Per-mode default run timeouts (seconds). MIRRORS grok_agent.py _add_run_opts()
// so direct mode honors the same deadlines as the hardened path; keep in sync.
const DIRECT_TIMEOUT_DEFAULTS_SECONDS = {
  code: 3600,
  verify: 1800,
  reason: 900,
  review: 900,
  "adversarial-review": 900,
};
// Hard ceiling mirrors runstate.MAX_RUN_TIMEOUT_SECONDS (the wrapper's clamp).
const DIRECT_MAX_TIMEOUT_SECONDS = 7 * 24 * 3600;

/** Resolve the effective direct-mode timeout: --timeout override, else per-mode
 *  default; junk/non-positive falls back to the default; clamped to the ceiling. */
export function resolveDirectTimeoutSeconds(args, mode) {
  const fallback = DIRECT_TIMEOUT_DEFAULTS_SECONDS[mode] ?? 900;
  const raw = flagValue(args, "--timeout");
  if (raw == null) return fallback;
  const n = Number.parseInt(String(raw), 10);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(n, DIRECT_MAX_TIMEOUT_SECONDS);
}

function toolsForMode(mode) {
  if (mode === "code") {
    return "read_file,write,search_replace,run_terminal_command,list_dir,grep";
  }
  if (mode === "verify") {
    return "read_file,run_terminal_command,list_dir,grep";
  }
  if (mode === "reason") {
    return "read_file,list_dir,grep";
  }
  // review / adversarial
  return "read_file,list_dir,grep";
}

// Web tool IDs the hardened grokcli path folds into the allowlist when web access
// is on (D-WEB contract). MIRRORS groklib/grokcli.py WEB_TOOLS; keep in sync.
const WEB_TOOLS = ["web_search", "web_fetch", "open_page", "open_page_with_find"];

/** The mode's tool allowlist, with the web tools appended when web is enabled -
 *  else the installed CLI runs ungrounded while the envelope reports webAccess. */
function toolsAllowlist(mode, web) {
  const base = toolsForMode(mode);
  return web ? `${base},${WEB_TOOLS.join(",")}` : base;
}

/**
 * Run a direct Grok CLI invocation. Returns { code, envelopeText }.
 */
/**
 * Redact a direct-mode envelope through the wrapper's SINGLE redaction source
 * (groklib.envelope + D4(a) injectedsecrets) so runMode=direct honors the same
 * stdout redaction contract as hardened mode. Loads operator ~/.grok auth leaves
 * via production AUTH_FILE_NAMES / register_injected_secrets_from_home (no
 * Node-side filename list), exact-masks those values first, then pattern-redacts
 * and fail-closed asserts. Auth unreadable -> empty denylist (pattern scan still
 * runs). Denylist always cleared in finally. Returns the redacted JSON string, or
 * null (fail closed) if redaction cannot run or a secret survives the scan.
 */
const REDACT_SCRIPT = [
  "import sys, json",
  "sys.path.insert(0, sys.argv[1])",
  // AUTH_FILE_NAMES + source_grok_dir are the production SSOTs (modes/_shared);
  // register/redact/clear live in injectedsecrets (same path hardened create_private_home uses).
  "from groklib.modes._shared import AUTH_FILE_NAMES, source_grok_dir",
  "from groklib.injectedsecrets import (",
  "    register_injected_secrets_from_home,",
  "    redact_injected_secrets,",
  "    clear_injected_secret_denylist,",
  ")",
  "from groklib.envelope import redact_secret_material, assert_no_secret_material",
  "obj = json.load(sys.stdin)",
  "try:",
  // Fail-safe: register never raises; unreadable/malformed auth yields empty denylist
  // without weakening the pattern scan / assert below.
  "    register_injected_secrets_from_home(source_grok_dir(), AUTH_FILE_NAMES)",
  "    red = redact_injected_secrets(obj)",
  "    red = redact_secret_material(red, redact_keys=True)",
  "    assert_no_secret_material(red)",
  "    sys.stdout.write(json.dumps(red))",
  "finally:",
  "    clear_injected_secret_denylist()",
].join("\n");

/** Run the wrapper's single-source redactor over a JSON payload; returns the
 *  redacted JSON string, or null if redaction cannot run / a secret survives. */
function runRedactor(payload, { scriptsDir, python, env }) {
  if (!scriptsDir || !python) return null;
  const res = spawnSync(python, ["-c", REDACT_SCRIPT, scriptsDir], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    env,
    maxBuffer: 64 * 1024 * 1024,
  });
  if (res.error || res.status !== 0 || !res.stdout) return null;
  return res.stdout;
}

function redactEnvelopeViaWrapper(envelope, opts) {
  return runRedactor(envelope, opts);
}

/** Redact a free-text string (e.g. installed-CLI stderr) through the same single
 *  source; returns the redacted text, or null when redaction cannot run. */
function redactTextViaWrapper(text, opts) {
  const out = runRedactor({ t: String(text) }, opts);
  if (out == null) return null;
  try {
    return JSON.parse(out).t;
  } catch {
    return null;
  }
}

export function runDirectGrok({
  mode,
  args,
  cwd,
  env = process.env,
  scriptsDir = null,
  python = null,
}) {
  // --isolated is wrapper-only (owned external worktree). Direct mode bypasses
  // the hardened parser and must not silently review the live tree instead.
  if (hasFlag(args, "--isolated")) {
    const envelope = {
      schemaVersion: 1,
      mode,
      status: "failure",
      runId: `direct-${Date.now()}`,
      error: {
        class: "isolation-unavailable",
        message:
          "review --isolated requires hardened mode (owned worktree isolation is not available under runMode=direct)",
        detail: {
          hint: "re-run without --isolated, or set GROK_SKILLS_MODE=hardened / companion setup --run-mode hardened",
        },
      },
      response: null,
      warnings: [
        "runMode=direct: --isolated is rejected fail-closed (no silent live-checkout fallback)",
      ],
      policy: { direct: true },
    };
    return { code: 1, envelopeText: `${JSON.stringify(envelope)}\n` };
  }

  // --input / --rules-file are hardened reason-mode assembly flags: the wrapper
  // reads each named artifact/rule file and folds it into the prompt. Direct mode
  // builds the prompt only from the task text, so honoring these silently would
  // return a "success" envelope that IGNORED every named input. Refuse fail-closed
  // rather than drop them (parity with the --isolated refusal above).
  if (
    args.some(
      (a) =>
        a === "--input" ||
        a === "--rules-file" ||
        (typeof a === "string" && (a.startsWith("--input=") || a.startsWith("--rules-file=")))
    )
  ) {
    const envelope = {
      schemaVersion: 1,
      mode,
      status: "failure",
      runId: `direct-${Date.now()}`,
      error: {
        class: "usage-error",
        message:
          "--input / --rules-file require hardened mode (runMode=direct does not assemble named artifacts or rule files into the prompt)",
        detail: {
          hint: "re-run without --input/--rules-file, or set GROK_SKILLS_MODE=hardened / companion setup --run-mode hardened",
        },
      },
      response: null,
      warnings: [
        "runMode=direct: --input/--rules-file rejected fail-closed (no silent drop of named artifacts/rules)",
      ],
      policy: { direct: true },
    };
    return { code: 1, envelopeText: `${JSON.stringify(envelope)}\n` };
  }

  // --schema is a structured-output contract: the hardened wrapper loads the
  // schema, passes it to Grok, AND validates the output (schema-mismatch failure).
  // Direct mode has none of that machinery, so honoring it would return a
  // "success" envelope with arbitrary unvalidated JSON/text. Refuse fail-closed.
  if (
    args.some(
      (a) => a === "--schema" || (typeof a === "string" && a.startsWith("--schema="))
    )
  ) {
    const envelope = {
      schemaVersion: 1,
      mode,
      status: "failure",
      runId: `direct-${Date.now()}`,
      error: {
        class: "usage-error",
        message:
          "--schema requires hardened mode (runMode=direct cannot load or validate a structured-output schema, so the result would be unvalidated)",
        detail: {
          hint: "re-run without --schema, or set GROK_SKILLS_MODE=hardened / companion setup --run-mode hardened",
        },
      },
      response: null,
      warnings: [
        "runMode=direct: --schema rejected fail-closed (no schema load/validation available)",
      ],
      policy: { direct: true },
    };
    return { code: 1, envelopeText: `${JSON.stringify(envelope)}\n` };
  }

  const binary = resolveGrokBinary(env);
  const task = extractTask(args);
  if (!task.trim() && mode !== "preflight") {
    const envelope = {
      schemaVersion: 1,
      mode,
      status: "failure",
      runId: `direct-${Date.now()}`,
      error: {
        class: "usage-error",
        message: "direct mode requires --task or --task-file",
        detail: null,
      },
      response: null,
      warnings: [
        "runMode=direct: using installed Grok CLI without grok-skills private-home sandbox isolation",
      ],
      policy: { direct: true },
    };
    return { code: 1, envelopeText: `${JSON.stringify(envelope)}\n` };
  }

  if (mode === "preflight") {
    const version = spawnSync(binary, ["--version"], { encoding: "utf8", env });
    const ok = version.status === 0;
    const envelope = {
      schemaVersion: 1,
      mode: "preflight",
      status: ok ? "success" : "failure",
      runId: `direct-preflight-${Date.now()}`,
      response: {
        checks: [
          {
            name: "grokBinary",
            ok,
            detail: ok ? (version.stdout || "").trim().split("\n")[0] : version.stderr || "grok --version failed",
          },
          {
            name: "runMode",
            ok: true,
            detail: "direct (installed Grok CLI; no private-home isolation)",
          },
        ],
      },
      warnings: [
        "runMode=direct: preflight only checks the installed binary; use hardened mode for full sandbox/auth-home probes",
      ],
      error: ok
        ? null
        : { class: "tool-unavailable", message: "grok CLI not usable", detail: version.stderr },
      policy: { direct: true },
    };
    return { code: ok ? 0 : 1, envelopeText: `${JSON.stringify(envelope)}\n` };
  }

  const model = flagValue(args, "--model") || "grok-4.5";
  // --worktree is a VERIFY-only flag (the retained worktree to inspect). Honor it
  // as the cwd ONLY for verify; for other direct modes it is not a valid flag and
  // must be ignored, or `code --target <consented A> --worktree <B>` would pass
  // the direct-consent gate on A yet point the CLI at B - a live edit of a repo
  // that never recorded direct consent. Non-verify modes fall back to --target.
  const cwdFlag =
    (mode === "verify" ? flagValue(args, "--worktree") : null) ||
    flagValue(args, "--target") ||
    cwd;
  // Equals-aware last-wins --web/--no-web (single source: companion-args).
  // null => default off for direct (no per-mode default table on this path).
  const webFlag = resolveWebFlag(args);
  const webRequested = webFlag === true;
  // Verify MUST stay hermetic: the hardened verify path never accepts --web and
  // the verify authority requires reproducible, network-free evidence. Force web
  // off for verify even if --web slipped through, so a direct verify can never
  // reach live web tools and still emit a normal verify envelope.
  const web = mode === "verify" ? false : webRequested;
  const webForcedOff = mode === "verify" && webRequested;
  // Stage the prompt with the shared 0600 helper (private mkdtemp dir): the task
  // text can carry transferred transcripts or pasted credentials, so it must not
  // sit world-readable under /tmp while the installed Grok CLI runs.
  const { taskPath: promptFile, cleanup: cleanupPrompt } = stageTaskFile(task);

  const argv = [
    "--prompt-file",
    promptFile,
    "--verbatim",
    "--cwd",
    path.resolve(cwdFlag),
    "--model",
    model,
    "--output-format",
    "json",
    "--permission-mode",
    "auto",
    "--tools",
    toolsAllowlist(mode, web),
    "--no-subagents",
    "--no-memory",
    "--no-plan",
  ];
  if (!web) {
    argv.push("--disable-web-search");
  }
  if (mode === "review" || mode === "adversarial-review" || mode === "reason") {
    argv.push("--sandbox", "read-only");
  } else if (mode === "code" || mode === "verify") {
    argv.push("--sandbox", "workspace");
  }
  // Forward an operator turn budget: the hardened path passes --max-turns to the
  // CLI, so direct must too, or a turn-capped run silently ignores the cap and
  // runs to the wall-clock timeout / normal completion.
  const maxTurnsRaw = flagValue(args, "--max-turns");
  if (maxTurnsRaw != null) {
    const n = Number.parseInt(String(maxTurnsRaw), 10);
    // Same [1, 100000] bound the hardened wrapper enforces (_MAX_TURNS); an
    // out-of-range/junk value is not forwarded rather than passed on unbounded.
    if (Number.isFinite(n) && n > 0 && n <= 100000) {
      argv.push("--max-turns", String(n));
    }
  }

  const timeoutSeconds = resolveDirectTimeoutSeconds(args, mode);
  const result = spawnSync(binary, argv, {
    cwd: path.resolve(cwd),
    encoding: "utf8",
    env,
    maxBuffer: 64 * 1024 * 1024,
    // Honor --timeout / the per-mode default so a hung or endless-stream Grok
    // CLI cannot block the companion in spawnSync forever (the hardened path
    // enforces the same deadlines). On expiry spawnSync kills the child.
    timeout: timeoutSeconds * 1000,
    killSignal: "SIGTERM",
  });
  const timedOut = Boolean(result.error && result.error.code === "ETIMEDOUT");

  cleanupPrompt();

  const raw = (result.stdout || "").trim();
  let responseText = raw;
  try {
    const parsed = JSON.parse(raw);
    responseText =
      parsed.result ??
      parsed.response ??
      parsed.message ??
      parsed.output ??
      raw;
    if (typeof responseText !== "string") {
      responseText = JSON.stringify(responseText, null, 2);
    }
  } catch {
    // keep raw
  }

  const ok = result.status === 0;
  const envelope = {
    schemaVersion: 1,
    mode,
    status: ok ? "success" : "failure",
    runId: `direct-${Date.now()}`,
    response: { text: responseText },
    warnings: [
      "runMode=direct: used installed Grok CLI without grok-skills private-home isolation or wrapper sandbox verification",
      ...(webForcedOff
        ? ["verify stays hermetic: --web ignored (no live web access on the direct verify path)"]
        : []),
    ],
    error: ok
      ? null
      : {
          // A wall-clock timeout is a distinct remediation from a missing/broken
          // binary; callers route tool-unavailable as "install/fix grok". Keep
          // the class aligned with grok.stopReason (and the hardened timeout class).
          class: timedOut ? "timeout" : "tool-unavailable",
          message: timedOut
            ? `grok CLI exceeded the ${timeoutSeconds}s timeout (runMode=direct); process killed`
            : (result.stderr || "grok exited non-zero").trim().slice(0, 2000),
          detail: { exitCode: result.status, timedOut },
        },
    policy: { direct: true, webAccess: web, model },
    grok: { stopReason: ok ? "end_turn" : timedOut ? "timeout" : "error" },
  };
  if (result.stderr) {
    // Redact stderr through the SAME single source before relaying to the
    // terminal: the installed CLI can echo a token it read from the repo/terminal
    // to stderr, and relaying raw bytes would leak it to terminal logs even though
    // the stdout envelope is redacted/withheld just below. Withhold on failure.
    const redactedErr = redactTextViaWrapper(result.stderr, { scriptsDir, python, env });
    if (redactedErr != null) {
      process.stderr.write(redactedErr.endsWith("\n") ? redactedErr : `${redactedErr}\n`);
    } else {
      process.stderr.write(
        "[runMode=direct: grok stderr withheld; secret redaction unavailable]\n"
      );
    }
  }
  // Redact the response through the wrapper's redaction (installed CLI output can
  // quote a token it read from the repo/terminal). Fail closed: if redaction
  // cannot run, withhold response.text rather than emit a possible secret.
  const redacted = redactEnvelopeViaWrapper(envelope, { scriptsDir, python, env });
  if (redacted) {
    return { code: ok ? 0 : 1, envelopeText: `${redacted}\n` };
  }
  // Fail closed on EVERY field derived from untrusted Grok output: response.text
  // AND error.message (built from grok stderr, which can echo a token). Emit a
  // minimal envelope that carries no unredacted model/stderr text.
  const withheld = {
    ...envelope,
    response: { text: "[redaction-unavailable: response withheld under runMode=direct]" },
    error: ok
      ? null
      : {
          class: (envelope.error && envelope.error.class) || "tool-unavailable",
          message: "[redaction-unavailable: error detail withheld under runMode=direct]",
          detail: { exitCode: (envelope.error && envelope.error.detail && envelope.error.detail.exitCode) ?? null },
        },
    warnings: [
      ...(envelope.warnings || []),
      "runMode=direct: secret redaction could not run; response/error text withheld",
    ],
  };
  return { code: ok ? 0 : 1, envelopeText: `${JSON.stringify(withheld)}\n` };
}

export function grokBinaryAvailable(env = process.env) {
  const binary = resolveGrokBinary(env);
  const result = spawnSync(binary, ["--version"], { encoding: "utf8", env });
  return {
    binary,
    ok: result.status === 0,
    version: (result.stdout || "").trim().split("\n")[0] || null,
    detail: result.status === 0 ? null : (result.stderr || "not found").trim(),
  };
}
