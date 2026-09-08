// plugin/scripts/lib/task-result.mjs
//
// Compact model-facing TaskResult projected from the existing envelope.
// Same success/failure author: envelope.status. Missing fields are not success.

const SUMMARY_MAX = 2000;
const SETUP_PURPOSES = new Set(["install", "setup"]);

function isCancelledEnvelope(envelope) {
  const reason = String(envelope?.stopReason || envelope?.error?.class || "").toLowerCase();
  return (
    envelope?.status === "cancelled" ||
    reason === "cancelled" ||
    reason === "cancel"
  );
}

function executionFromEnvelope(envelope) {
  if (isCancelledEnvelope(envelope)) return "cancelled";
  if (envelope?.incompleteStop === true) return "incomplete";
  const processExit = envelope?.processExit ?? envelope?.exitStatus;
  if (typeof processExit === "number" && processExit !== 0) return "failed";
  const status = String(envelope?.status || "");
  if (status === "success") return "completed";
  if (status === "cancelled") return "cancelled";
  return "failed";
}

function applicationFromEnvelope(envelope) {
  const integration = envelope?.integration || envelope?.response?.integration;
  const mode = String(envelope?.mode || "");
  if (mode === "review" || mode === "reason") {
    return "not-applicable";
  }
  if (integration?.applied === true) return "applied";
  if (integration?.ready === true && integration?.applied !== true) return "patch-ready";
  if (envelope?.worktreePath) return "not-applicable";
  if (mode === "code" || mode === "direct") return "in-place";
  return "not-applicable";
}

function isVerificationCommand(item) {
  const purpose = String(item?.purpose || "").toLowerCase();
  if (SETUP_PURPOSES.has(purpose)) return false;
  const argv = Array.isArray(item?.argv) ? item.argv.map((part) => String(part)) : [];
  if (argv.includes("install") || argv.includes("ci")) return false;
  return true;
}

function numericExit(item) {
  const raw = item?.exitStatus;
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

function verificationFromEnvelope(envelope) {
  if (envelope?.error?.class === "validation-failure") return "failed";
  if (executionFromEnvelope(envelope) !== "completed") return "not-run";
  const commands = envelope?.commands || envelope?.response?.commands;
  if (!Array.isArray(commands) || commands.length === 0) return "not-run";
  const checks = commands.filter(isVerificationCommand);
  if (!checks.length) return "not-run";
  const exits = checks.map(numericExit);
  if (exits.some((code) => code === null)) return "not-run";
  if (exits.some((code) => code !== 0)) return "failed";
  return "passed";
}

function workspacePathFromEnvelope(envelope) {
  const target = envelope?.targetWorkspace;
  if (typeof target === "string" && target.trim()) return target;
  if (envelope?.worktreePath) return envelope.worktreePath;
  if (envelope?.effectiveWorkingDirectory) return envelope.effectiveWorkingDirectory;
  if (envelope?.repository) return envelope.repository;
  return null;
}

function boundSummary(text) {
  const trimmed = String(text || "").trim();
  if (trimmed.length <= SUMMARY_MAX) return trimmed;
  return `${trimmed.slice(0, SUMMARY_MAX)}\n…(truncated)`;
}

export function projectTaskResult(envelope) {
  const env = envelope && typeof envelope === "object" ? envelope : {};
  const text =
    typeof env.response === "string"
      ? env.response
      : typeof env.response?.text === "string"
        ? env.response.text
        : "";
  const isolated = Boolean(env.worktreePath);
  return {
    protocolVersion: 2,
    runId: env.runId || null,
    execution: executionFromEnvelope(env),
    workspace: {
      path: workspacePathFromEnvelope(env),
      owner: isolated ? "plugin" : "host",
      placement: isolated ? "isolated" : "inherit",
    },
    verification: {
      state: verificationFromEnvelope(env),
    },
    application: applicationFromEnvelope(env),
    changedFiles: Array.isArray(env.changedFiles) ? env.changedFiles : [],
    summary: boundSummary(text),
    blockers: Array.isArray(env.response?.integration?.blockers)
      ? env.response.integration.blockers
      : [],
    artifactPaths: [],
  };
}
