// plugin/scripts/lib/task-result.mjs
//
// Compact model-facing TaskResult projected from the existing envelope.
// Same success/failure author: envelope.status. Missing fields are not success.

function executionFromEnvelope(envelope) {
  const status = String(envelope?.status || "");
  if (status === "success") return "completed";
  if (status === "cancelled" || envelope?.error?.class === "cancelled") return "cancelled";
  return "failed";
}

function applicationFromEnvelope(envelope) {
  const integration = envelope?.integration || envelope?.response?.integration;
  if (envelope?.mode === "review" || envelope?.mode === "reason") {
    return "not-applicable";
  }
  if (integration?.applied === true) return "applied";
  if (integration?.ready === true && integration?.applied !== true) return "patch-ready";
  if (envelope?.worktreePath) return "not-applicable";
  if (String(envelope?.mode || "") === "code") return "in-place";
  return "not-applicable";
}

function verificationFromEnvelope(envelope) {
  if (envelope?.error?.class === "validation-failure") return "failed";
  const commands = envelope?.commands || envelope?.response?.commands;
  if (Array.isArray(commands) && commands.length) {
    if (commands.some((item) => Number(item?.exitStatus) !== 0)) return "failed";
    if (envelope?.status === "success") return "passed";
  }
  return "not-run";
}

export function projectTaskResult(envelope) {
  const env = envelope && typeof envelope === "object" ? envelope : {};
  const text =
    typeof env.response === "string"
      ? env.response
      : typeof env.response?.text === "string"
        ? env.response.text
        : "";
  return {
    protocolVersion: 2,
    runId: env.runId || null,
    execution: executionFromEnvelope(env),
    workspace: {
      path: env.worktreePath || env.targetWorkspace || null,
      owner: env.worktreePath ? "plugin" : "host",
      placement: env.worktreePath ? "isolated" : "inherit",
    },
    verification: {
      state: verificationFromEnvelope(env),
    },
    application: applicationFromEnvelope(env),
    changedFiles: Array.isArray(env.changedFiles) ? env.changedFiles : [],
    summary: String(text).trim(),
    blockers: Array.isArray(env.response?.integration?.blockers)
      ? env.response.integration.blockers
      : [],
    artifactPaths: [],
  };
}
