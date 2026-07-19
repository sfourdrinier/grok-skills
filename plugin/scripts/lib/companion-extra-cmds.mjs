// plugin/scripts/lib/companion-extra-cmds.mjs
//
// Companion-only commands extracted from grok-companion.mjs (900-line cap).

import process from "node:process";

import { extractTask, injectTaskFile } from "./task-file.mjs";
import {
  getJob,
  readJobStdout,
} from "./jobs.mjs";
import {
  buildTransferTaskBody,
  readSessionStamp,
  resolveTransferSource,
  writeTransferPack,
} from "./session-stamp.mjs";

export function cmdTransfer(cwd, args) {
  let source = null;
  let force = false;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--source" && args[i + 1]) {
      source = args[++i];
    } else if (args[i] === "--force") {
      force = true;
    }
  }
  let sessionPath =
    source ||
    process.env.GROK_CLAUDE_SESSION_PATH ||
    process.env.CLAUDE_SESSION_PATH ||
    "";
  if (!sessionPath) {
    const stamp = readSessionStamp(cwd, process.env);
    if (stamp?.transcript_path) {
      sessionPath = stamp.transcript_path;
    }
  }
  if (!sessionPath) {
    process.stderr.write(
      "[grok-companion] transfer needs a Claude session jsonl.\n" +
        "Pass --source <path> or ensure SessionStart recorded a workspace stamp.\n"
    );
    return 1;
  }
  const resolved = resolveTransferSource(sessionPath, { force, env: process.env });
  if (!resolved.ok) {
    process.stderr.write(`[grok-companion] transfer refused: ${resolved.reason}\n`);
    return 1;
  }
  let body;
  try {
    body = buildTransferTaskBody(resolved.path);
  } catch (err) {
    process.stderr.write(`[grok-companion] could not read session: ${err.message}\n`);
    return 1;
  }
  const taskPath = writeTransferPack(body, process.env);
  process.stdout.write(
    [
      "Transfer pack ready.",
      `session: ${resolved.path}`,
      `task-file: ${taskPath}`,
      "",
      "Continue with:",
      `  node \"$GROK_PLUGIN_ROOT/scripts/grok-companion.mjs\" reason --task-file '${taskPath}'`,
      "or",
      `  node \"$GROK_PLUGIN_ROOT/scripts/grok-companion.mjs\" code --target . --base HEAD --task-file '${taskPath}'`,
      "",
    ].join("\n")
  );
  return 0;
}

export async function cmdDebate(cwd, wrapper, args, runMode, captureAndTrack) {
  const task = extractTask(args) || "Debate the design tradeoffs in this repository.";
  const round1 = [
    "You are side A in a structured debate. Argue your position clearly with",
    "concrete evidence from the repo or supplied artifacts.",
    "",
    task,
  ].join("\n");
  const inj1 = injectTaskFile(["reason"], round1);
  const code1 = await captureAndTrack(wrapper, inj1.args, {
    cwd, mode: "reason", kind: "debate-a", runMode, skipNotify: true,
  });
  inj1.cleanup();
  if (code1 !== 0) return code1;
  const last = getJob(cwd, null);
  const prior = last ? readJobStdout(cwd, last.id) : "";
  const round2 = [
    "You are side B in a structured debate. Your job is to DISAGREE where",
    "warranted, steelman the other side, and name residual risks.",
    "",
    "## Side A output",
    prior || "(missing)",
    "",
    "## Original topic",
    task,
    "",
    "End with: agreement points, disagreements, and a recommended resolution.",
  ].join("\n");
  const inj2 = injectTaskFile(["reason"], round2);
  const code2 = await captureAndTrack(wrapper, inj2.args, {
    cwd, mode: "reason", kind: "debate-b", runMode,
  });
  inj2.cleanup();
  return code2;
}
