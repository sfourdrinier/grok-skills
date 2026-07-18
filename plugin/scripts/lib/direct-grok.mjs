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
import { randomBytes } from "node:crypto";

import { extractTask } from "./task-file.mjs";

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

function flagValue(args, name) {
  // Accept BOTH `--name value` and `--name=value`, last-wins (matches the
  // wrapper's argparse and the consent parser), so runMode=direct executes
  // against the same --target the consent gate was keyed on (review).
  const eq = name + "=";
  let val = null;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === name && args[i + 1] !== undefined) {
      val = args[i + 1];
    } else if (typeof a === "string" && a.startsWith(eq)) {
      val = a.slice(eq.length);
    }
  }
  return val;
}

function hasFlag(args, name) {
  return args.includes(name);
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

/**
 * Run a direct Grok CLI invocation. Returns { code, envelopeText }.
 */
/**
 * Redact a direct-mode envelope through the wrapper's SINGLE redaction source
 * (groklib.envelope) so runMode=direct honors the same stdout redaction contract
 * as hardened mode. Returns the redacted JSON string, or null (fail closed) if
 * redaction cannot run or a secret survives the scan.
 */
function redactEnvelopeViaWrapper(envelope, { scriptsDir, python, env }) {
  if (!scriptsDir || !python) return null;
  const script =
    "import sys,json;" +
    "sys.path.insert(0, sys.argv[1]);" +
    "from groklib.envelope import redact_secret_material, assert_no_secret_material;" +
    "obj=json.load(sys.stdin);" +
    "red=redact_secret_material(obj, redact_keys=True);" +
    "assert_no_secret_material(red);" +
    "sys.stdout.write(json.dumps(red))";
  const res = spawnSync(python, ["-c", script, scriptsDir], {
    input: JSON.stringify(envelope),
    encoding: "utf8",
    env,
    maxBuffer: 64 * 1024 * 1024,
  });
  if (res.error || res.status !== 0 || !res.stdout) return null;
  return res.stdout;
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
  const cwdFlag = flagValue(args, "--target") || cwd;
  const web = hasFlag(args, "--web");
  const promptFile = path.join(
    os.tmpdir(),
    `grok-skills-direct-${randomBytes(4).toString("hex")}.md`
  );
  fs.writeFileSync(promptFile, task, "utf8");

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
    toolsForMode(mode),
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

  const result = spawnSync(binary, argv, {
    cwd: path.resolve(cwd),
    encoding: "utf8",
    env,
    maxBuffer: 64 * 1024 * 1024,
  });

  try {
    fs.unlinkSync(promptFile);
  } catch {
    // ignore
  }

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
    ],
    error: ok
      ? null
      : {
          class: "tool-unavailable",
          message: (result.stderr || "grok exited non-zero").trim().slice(0, 2000),
          detail: { exitCode: result.status },
        },
    policy: { direct: true, webAccess: web, model },
    grok: { stopReason: ok ? "end_turn" : "error" },
  };
  if (result.stderr) {
    process.stderr.write(result.stderr);
  }
  // Redact the response through the wrapper's redaction (installed CLI output can
  // quote a token it read from the repo/terminal). Fail closed: if redaction
  // cannot run, withhold response.text rather than emit a possible secret.
  const redacted = redactEnvelopeViaWrapper(envelope, { scriptsDir, python, env });
  if (redacted) {
    return { code: ok ? 0 : 1, envelopeText: `${redacted}\n` };
  }
  const withheld = {
    ...envelope,
    response: { text: "[redaction-unavailable: response withheld under runMode=direct]" },
    warnings: [
      ...(envelope.warnings || []),
      "runMode=direct: secret redaction could not run; response text withheld",
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
