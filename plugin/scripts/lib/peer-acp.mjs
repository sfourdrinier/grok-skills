// plugin/scripts/lib/peer-acp.mjs
//
// Experimental ACP peer channel companion helpers (gate + peer-start background).

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

import { wrapperChildEnv } from "./notify.mjs";

export const PEER_MODES = new Set(["peer", "peer-start", "peer-prompt", "peer-stop"]);
export const ACP_SPEC_POINTER = "docs/specs/2026-07-17-acp-peer-channel-design.md";

export function isPeerMode(mode) {
  return PEER_MODES.has(mode) || String(mode || "").startsWith("peer-");
}

export function refusePeerExperimental(mode) {
  process.stderr.write(
    `[grok-companion] peer mode '${mode}' is experimental and refused unless ` +
      `GROK_EXPERIMENTAL_ACP=1 (see ${ACP_SPEC_POINTER}).\n`
  );
  return 1;
}

export function refusePeerDirect(mode) {
  process.stderr.write(
    `[grok-companion] peer mode '${mode}' requires hardened mode (fail closed).\n`
  );
  return 1;
}

/**
 * peer-start: launch the resident wrapper in the background, relay its one
 * "running" envelope, leave the process serving the control socket.
 */
export function runPeerStartBackground(python, wrapper, args, {
  spawnFailedMessage,
  signalExit = 1,
  spawnFailedExit = 4,
}) {
  return new Promise((resolve) => {
    let stdoutBuf = "";
    let settled = false;
    const finish = (code) => {
      if (settled) return;
      settled = true;
      resolve(code);
    };
    let child;
    // Resident stderr goes to a DURABLE LOG FILE, not a pipe and not /dev/null:
    // the companion exits after the first running envelope, which would break a
    // stderr pipe on a later resident write (log_stderr uses raw os.write ->
    // could crash the peer); /dev/null would instead lose the diagnostics for a
    // pre-envelope resident crash. A file fd survives the companion exit and
    // stays inspectable. The parent closes its copy after spawn (child keeps its
    // own dup); falls back to "ignore" if the log cannot be opened.
    let errTarget = "ignore";
    let errLogPath = null;
    try {
      errLogPath = path.join(os.tmpdir(), `grok-peer-start-${process.pid}-${Date.now()}.log`);
      errTarget = fs.openSync(errLogPath, "a");
    } catch {
      errTarget = "ignore";
      errLogPath = null;
    }
    try {
      child = spawn(python, [wrapper, ...args], {
        stdio: ["ignore", "pipe", errTarget],
        env: wrapperChildEnv(process.env),
        detached: true,
      });
      if (typeof errTarget === "number") {
        try {
          fs.closeSync(errTarget);
        } catch {
          /* child holds its own dup of the fd */
        }
      }
    } catch (err) {
      if (typeof errTarget === "number") {
        try {
          fs.closeSync(errTarget);
        } catch {
          /* ignore */
        }
      }
      process.stderr.write(spawnFailedMessage(wrapper, err.message));
      finish(spawnFailedExit);
      return;
    }
    if (child.stderr) {
      child.stderr.setEncoding("utf8");
      child.stderr.on("data", (chunk) => process.stderr.write(chunk));
    }
    if (child.stdout) {
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk) => {
        stdoutBuf += chunk;
        const nl = stdoutBuf.indexOf("\n");
        if (nl >= 0 && !settled) {
          const line = stdoutBuf.slice(0, nl + 1);
          process.stdout.write(line.endsWith("\n") ? line : `${line}\n`);
          // peer-start emits exactly one envelope. status "running" = the
          // resident session is up (background it, exit 0). Any other status is
          // a pre-resident failure (auth/probe/usage) and must NOT look like a
          // successful start - propagate a nonzero exit.
          let running = false;
          try {
            running = JSON.parse(line.trim()).status === "running";
          } catch {
            running = false;
          }
          if (running) {
            try {
              child.unref();
            } catch {
              /* ignore */
            }
            finish(0);
          } else {
            finish(signalExit);
          }
        }
      });
    }
    child.on("error", (err) => {
      process.stderr.write(spawnFailedMessage(wrapper, err.message));
      finish(spawnFailedExit);
    });
    child.on("close", (code, signal) => {
      if (settled) return;
      if (stdoutBuf.trim()) {
        process.stdout.write(stdoutBuf.endsWith("\n") ? stdoutBuf : `${stdoutBuf}\n`);
      } else if (errLogPath) {
        // Pre-envelope resident crash: point at the durable stderr log.
        process.stderr.write(
          `[grok-companion] peer-start produced no envelope; resident diagnostics: ${errLogPath}\n`
        );
      }
      if (typeof code === "number") {
        finish(code);
        return;
      }
      process.stderr.write(
        `[grok-companion] peer-start terminated by signal ${signal ?? "unknown"}.\n`
      );
      finish(signalExit);
    });
  });
}

/** Normalize `peer <start|prompt|stop>` into peer-* mode + rest args. */
export function normalizePeerArgs(mode, rest) {
  if (mode !== "peer") {
    return { mode, rest, error: null };
  }
  const sub = rest[0];
  if (sub === "start" || sub === "prompt" || sub === "stop") {
    return { mode: `peer-${sub}`, rest: rest.slice(1), error: null };
  }
  return {
    mode,
    rest,
    error: "[grok-companion] usage: peer <start|prompt|stop> [args...]\n",
  };
}
