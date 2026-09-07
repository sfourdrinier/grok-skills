// plugin/scripts/lib/execution-policy.mjs
//
// F06: resolve workspace / transport / application / validation from existing
// skill + integration flags. Illegal combos fail before spawn. Companion
// applyExecutionPolicyToArgs is the production entry that injects --validation.

import { hasFlagOrEquals } from "./companion-args.mjs";

const ISOLATING_MODES = new Set(["review", "worktree", "auto"]);
const VALIDATION_SKILLS = new Set(["code", "implement"]);

/**
 * @param {{
 *   skill: string,
 *   integrationMode?: string,
 *   isolationRequested?: boolean,
 *   continueRunId?: string|null,
 * }} input
 */
export function resolveExecutionPolicy({
  skill,
  integrationMode = "direct",
  isolationRequested = false,
  continueRunId = null,
} = {}) {
  const mode = String(integrationMode || "direct").toLowerCase();
  const isolating =
    isolationRequested === true || ISOLATING_MODES.has(mode) || skill === "implement";
  const transport = continueRunId ? "session" : "oneshot";

  if (skill === "review" || skill === "reason" || skill === "adversarial-review") {
    return {
      workspace: isolationRequested ? "isolated" : "inherit",
      transport: "oneshot",
      application: "not-applicable",
      validation: "targeted",
    };
  }

  if (skill === "implement") {
    return {
      workspace: "isolated",
      transport,
      application: "retain-patch",
      validation: "full",
    };
  }

  if (isolating) {
    return {
      workspace: "isolated",
      transport,
      application: mode === "auto" ? "apply-when-verified" : "retain-patch",
      validation: "targeted",
    };
  }

  return {
    workspace: "inherit",
    transport,
    application: "in-place",
    validation: "targeted",
  };
}

/**
 * Resolve policy and inject `--validation` for code/implement unless the
 * caller already set it. Does not rewrite `--integration`.
 *
 * @param {{
 *   skill: string,
 *   args?: string[],
 *   integrationMode?: string,
 *   isolationRequested?: boolean,
 *   continueRunId?: string|null,
 * }} input
 * @returns {{ policy: ReturnType<typeof resolveExecutionPolicy>, args: string[] }}
 */
export function applyExecutionPolicyToArgs({
  skill,
  args = [],
  integrationMode = "direct",
  isolationRequested = false,
  continueRunId = null,
} = {}) {
  const policy = resolveExecutionPolicy({
    skill,
    integrationMode,
    isolationRequested,
    continueRunId,
  });
  const next = Array.isArray(args) ? [...args] : [];
  if (VALIDATION_SKILLS.has(skill) && !hasFlagOrEquals(next, "--validation")) {
    next.push("--validation", policy.validation);
  }
  return { policy, args: next };
}
