// plugin/scripts/lib/integration-gate.mjs
//
// Code/implement integration gate + continue-run target resolution. Extracted
// from jobs.mjs to keep the jobs registry under the 900-line cap. Uses the
// companion argv SSOT for flag strip/value and jobs prefs for integration mode.
// No per-repo "consent" gate: direct lands when selected (like other providers).

import fs from "node:fs";
import path from "node:path";

import { dropValueFlags, flagValue, hasFlagOrEquals } from "./companion-args.mjs";
import { isDirectRunId } from "./direct-grok.mjs";
import {
  parseTargetFlag,
  resolveTargetWorkspaceRoot,
} from "./git-context.mjs";
import {
  getIntegrationMode,
  parseIntegrationMode,
} from "./jobs.mjs";
import { runsDirFor, safeRunIdForRunsDir } from "../progress-relay.mjs";

/**
 * Drop any existing --integration flag(s) then append the resolved effective mode.
 * Ensures the wrapper never silently defaults behind the companion gate.
 * @param {string[]} args
 * @param {string} mode
 * @returns {string[]}
 */
export function withExplicitIntegration(args, mode) {
  // Shared strip: never consume a following flag as --integration's value.
  const out = dropValueFlags(Array.isArray(args) ? args : [], ["--integration"]);
  out.push("--integration", mode);
  return out;
}

/**
 * Resolve the apply/target workspace for a continue-run. --target is forbidden
 * on the wrapper continuation path, so the companion derives the repo from the
 * prior run's durable metadata (run.json targetWorkspace/repository).
 * Relative targetWorkspace values (e.g. package "pkg") resolve against the
 * recorded rec.repository, never companion cwd - operators often continue from
 * outside the original checkout.
 * Falls back to companion cwd when the prior run is missing or unreadable.
 * @param {string} continueRunId
 * @param {string} cwd companion cwd (fallback when metadata is missing)
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string}
 */
export function resolveContinueRunTargetWorkspace(continueRunId, cwd, env = process.env) {
  const runsDir = runsDirFor(env);
  const safe = safeRunIdForRunsDir(continueRunId, runsDir);
  if (safe) {
    try {
      const raw = fs.readFileSync(path.join(runsDir, safe, "run.json"), "utf8");
      const rec = JSON.parse(raw);
      if (rec && typeof rec === "object" && !Array.isArray(rec)) {
        const tw =
          typeof rec.targetWorkspace === "string" && rec.targetWorkspace.trim()
            ? rec.targetWorkspace.trim()
            : "";
        const repo =
          typeof rec.repository === "string" && rec.repository.trim()
            ? rec.repository.trim()
            : "";
        if (tw) {
          // Absolute tw uses itself; relative tw is package-relative to the
          // recorded repository (wrapper targetWorkspace SSOT).
          const base = repo || cwd;
          return resolveTargetWorkspaceRoot(base, tw);
        }
        if (repo) {
          return resolveTargetWorkspaceRoot(cwd, repo);
        }
      }
    } catch {
      // Missing/unreadable prior run: fall through to cwd-scoped default.
    }
  }
  return resolveTargetWorkspaceRoot(cwd, ".");
}

/**
 * Resolve effective integration for code/implement. IntegrationMode is keyed on
 * the resolved TARGET repo root (git toplevel of --target, defaulting to '.'),
 * not companion cwd - so prefs for repo A do not silently switch mode for repo B.
 * No consent gate: direct lands when selected (2.0.1+).
 *
 * continue-run still resolves configured/explicit integration (auto keeps
 * apply-on-ready; review retains; direct maps wrapper worktree lineage without
 * auto apply). Apply target from prior-run metadata.
 *
 * @returns {{
 *   ok: true,
 *   effective: string|null,
 *   rest: string[],
 *   targetWorkspace?: string,
 *   continueRun?: boolean,
 * } | { ok: false, code: number, message: string }}
 */
export function gateIntegrationForCodeish(mode, rest, integrationFlag, cwd, env = process.env) {
  if (mode !== "code" && mode !== "implement") {
    return { ok: true, rest, effective: null };
  }
  const continueRunId = flagValue(rest, "--continue-run");
  const isContinueRun =
    continueRunId != null || hasFlagOrEquals(rest, "--continue-run");
  // Synthetic runMode=direct job ids (direct-<ms>) have no durable run record
  // under runs/; continue-run can only resume hardened lineages.
  if (isContinueRun && isDirectRunId(continueRunId || "")) {
    return {
      ok: false,
      code: 1,
      message:
        "[grok-companion] --continue-run requires a hardened run id " +
        "(from a prior hardened code envelope under ~/.local/state/grok-skills/runs/), " +
        "not a synthetic runMode=direct id (direct-*). " +
        "runMode=direct does not store continuable lineage. " +
        "Recovery: checkpoint or clean overlapping dirty paths, then start a " +
        "fresh hardened code run (setup --run-mode hardened; integration may " +
        "still be direct for live-tree landing).\n",
    };
  }
  // implement is verify-only (code + handoff, never applies to the live tree),
  // so it ALWAYS takes the worktree path: never direct (which would record the
  // code leg as mode=direct and make the immediate handoff refuse it after
  // mutating the tree).
  if (mode === "implement") {
    return { ok: true, effective: "worktree", rest: withExplicitIntegration(rest, "worktree") };
  }
  // Key integration prefs on the repo being edited, not process.cwd().
  // continue-run forbids --target, so key mode on the prior run's repo.
  const targetWorkspace = isContinueRun
    ? resolveContinueRunTargetWorkspace(continueRunId || "", cwd, env)
    : resolveTargetWorkspaceRoot(cwd, parseTargetFlag(rest));
  let effective;
  if (integrationFlag != null && String(integrationFlag).trim() !== "") {
    const parsed = parseIntegrationMode(integrationFlag);
    if (!parsed) {
      return {
        ok: false,
        code: 1,
        message:
          `[grok-companion] invalid --integration ${JSON.stringify(integrationFlag)} ` +
          `(valid: direct|worktree|auto|review)\n`,
      };
    }
    effective = parsed;
  } else {
    effective = getIntegrationMode(targetWorkspace, env);
  }
  // Wrapper continue-run always reuses worktree lineage. Companion auto/review
  // stay companion-side (apply-on-ready / retain); direct continues without
  // applying via auto and without rewriting the wrapper to live-edit.
  const wrapperIntegration =
    isContinueRun && (effective === "direct" || effective === "auto" || effective === "review")
      ? "worktree"
      : effective;
  return {
    ok: true,
    effective,
    rest: withExplicitIntegration(rest, wrapperIntegration),
    targetWorkspace,
    continueRun: Boolean(isContinueRun),
  };
}
