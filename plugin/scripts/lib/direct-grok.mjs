// plugin/scripts/lib/direct-grok.mjs
//
// "Use your installed Grok CLI" posture (parity with OpenAI codex-plugin-cc
// using the installed Codex). Spawns the real `grok` binary with the operator
// home/auth. No private-home isolation and no wrapper sandbox verification.
// Emits a lightweight envelope so skills still see JSON on stdout.
// Also owns direct-mode handoff refusal copy (Task 1.6) and the implement
// combo (Task 1.4) so grok-companion.mjs stays under the 900-line cap.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomBytes } from "node:crypto";

import { sanitizeRunId } from "./companion-terminal-notify.mjs";
import { wrapperChildEnv } from "./notify.mjs";
import { tryParseEnvelope } from "./render.mjs";

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
 * Capture handoff stdout so implement can read response.integration.ready.
 * Relays stderr + stdout like a passthrough; returns parsed envelope.
 */
export function runHandoffCaptured(wrapper, args, {
  python = process.env.GROK_PYTHON?.trim() || "python3",
  spawnFailedExit = 4,
  signalExit = 1,
  spawnFailedMessage = (w, d) =>
    `[grok-companion] failed to launch ${python} ${w}: ${d}\n`,
} = {}) {
  const result = spawnSync(python, [wrapper, ...args], {
    encoding: "utf8",
    env: wrapperChildEnv(process.env),
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) {
    process.stderr.write(spawnFailedMessage(wrapper, result.error.message));
    return { code: spawnFailedExit, envelope: null };
  }
  if (result.stderr) process.stderr.write(result.stderr);
  const stdout = result.stdout || "";
  if (stdout) process.stdout.write(stdout.endsWith("\n") ? stdout : `${stdout}\n`);
  return {
    code: typeof result.status === "number" ? result.status : signalExit,
    envelope: tryParseEnvelope(stdout),
  };
}

/**
 * One-call implement: code (live relay) then handoff. Exit 0 only when code
 * exit 0 AND handoff exit 0 AND response.integration.ready === true.
 * Direct mode is refused before any wrapper spawn.
 */
export async function runImplementCombo(wrapper, rest, runMode, track, {
  runWithLiveRelay,
  stderrLine = (line) => process.stderr.write(`${line}\n`),
} = {}) {
  if (runMode === "direct") {
    return writeDirectNoHandoffRefuse();
  }
  const codeArgs = ["code", ...rest];
  const res = await runWithLiveRelay(wrapper, codeArgs, { ...track, captureStdout: true });
  const code = typeof res === "number" ? res : res.code;
  const stdoutBuf = typeof res === "number" ? "" : res.stdout || "";
  const env = tryParseEnvelope(stdoutBuf);
  const runId = sanitizeRunId(env?.runId);
  if (!runId) {
    process.stderr.write(
      "[grok-companion] implement: no runId in the code envelope; cannot hand off.\n"
    );
    return code === 0 ? 1 : code;
  }
  stderrLine(`[grok-implement] code finished (exit ${code}); verifying handoff for ${runId}`);
  const { code: hCode, envelope: hEnv } = runHandoffCaptured(wrapper, [
    "handoff",
    "--run-id",
    runId,
  ]);
  // Real handoff success shape (modes/handoff.py): response.integration.ready
  const ready = hEnv?.response?.integration?.ready === true;
  stderrLine(`[grok-implement] handoff ${ready ? "READY" : "NOT READY"} for ${runId}`);
  return code === 0 && hCode === 0 && ready ? 0 : 1;
}

function resolveGrokBinary(env = process.env) {
  const override = (env.GROK_AGENT_BINARY ?? env.GROK_BINARY ?? "").trim();
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

function readTask(args) {
  const idx = args.indexOf("--task-file");
  if (idx >= 0 && args[idx + 1]) {
    return fs.readFileSync(args[idx + 1], "utf8");
  }
  const t = args.indexOf("--task");
  if (t >= 0 && args[t + 1]) {
    return args[t + 1];
  }
  return "";
}

function flagValue(args, name) {
  const i = args.indexOf(name);
  if (i >= 0 && args[i + 1]) {
    return args[i + 1];
  }
  return null;
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
export function runDirectGrok({ mode, args, cwd, env = process.env }) {
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
  const task = readTask(args);
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
  return { code: ok ? 0 : 1, envelopeText: `${JSON.stringify(envelope)}\n` };
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
