// plugin/scripts/lib/direct-grok.mjs
//
// "Use your installed Grok CLI" posture (parity with OpenAI codex-plugin-cc
// using the installed Codex). Spawns the real `grok` binary with the operator
// home/auth. No private-home isolation and no wrapper sandbox verification.
// Emits a lightweight envelope so skills still see JSON on stdout.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomBytes } from "node:crypto";

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
    runId: `direct-${Date.now().toString(16)}`,
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
