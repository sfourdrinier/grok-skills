// plugin/scripts/lib/git-context.mjs
// Resolve default review targets from git (working tree / branch base).

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { resolveWorkspaceRoot } from "./gate-state.mjs";

function git(cwd, args) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" });
  return {
    code: result.status ?? 1,
    out: (result.stdout ?? "").trim(),
    err: (result.stderr ?? "").trim(),
  };
}

export function isGitRepo(cwd) {
  return git(cwd, ["rev-parse", "--is-inside-work-tree"]).out === "true";
}

/**
 * Absolute path of the first --target value in argv (supports --target=).
 * Defaults to "." when absent.
 * @param {string[]} args
 * @returns {string}
 */
export function parseTargetFlag(args) {
  if (!Array.isArray(args)) return ".";
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--target" && args[i + 1] !== undefined) {
      const v = String(args[i + 1]);
      if (v.startsWith("-")) return ".";
      return v;
    }
    if (typeof a === "string" && a.startsWith("--target=")) {
      const v = a.slice("--target=".length);
      return v || ".";
    }
  }
  return ".";
}

/**
 * Resolve the workspace key for a code/setup --target.
 *
 * Absolute target path relative to companion cwd, then git toplevel when the
 * target sits in a repo (via resolveWorkspaceRoot / .git walk, same keying as
 * jobs state). Non-git targets fall back to the absolute target dir itself so
 * consent stays per-target and never silently uses companion cwd.
 *
 * @param {string} cwd companion process cwd
 * @param {string} [target="."] --target value (relative or absolute)
 * @returns {string} absolute workspace root used for prefs/consent keying
 */
export function resolveTargetWorkspaceRoot(cwd, target = ".") {
  const raw = target == null || String(target).trim() === "" ? "." : String(target);
  const absTarget = path.isAbsolute(raw)
    ? path.resolve(raw)
    : path.resolve(cwd || process.cwd(), raw);
  // Prefer git rev-parse when available (handles worktrees); fall back to the
  // .git walk used by jobs state so keying stays aligned either way.
  let probe = absTarget;
  while (!fs.existsSync(probe)) {
    const parent = path.dirname(probe);
    if (parent === probe) break;
    probe = parent;
  }
  if (fs.existsSync(probe)) {
    const top = git(probe, ["rev-parse", "--show-toplevel"]);
    if (top.code === 0 && top.out) {
      return path.resolve(top.out);
    }
  }
  return resolveWorkspaceRoot(absTarget);
}

export function shortstat(cwd, range = null) {
  const args = range
    ? ["diff", "--shortstat", `${range}...HEAD`]
    : ["diff", "--shortstat"];
  const a = git(cwd, args);
  const b = range ? { out: "" } : git(cwd, ["diff", "--shortstat", "--cached"]);
  const status = git(cwd, ["status", "--short", "--untracked-files=all"]);
  return {
    unstaged: a.out,
    staged: b.out,
    status: status.out,
    dirty: Boolean(status.out || a.out || b.out),
  };
}

export function defaultReviewTarget(cwd) {
  if (!isGitRepo(cwd)) {
    return { target: ".", reason: "not a git repo; using ." };
  }
  return { target: ".", reason: "repository root (working tree)" };
}

export function buildBranchReviewTask(base, userTask) {
  const focus = (userTask ?? "").trim();
  const head = [
    "Review the git branch changes relative to base revision:",
    `  base: ${base}`,
    "Use git history and diffs against that base. Focus on correctness bugs,",
    "security issues, regressions, and incomplete work introduced since the base.",
  ].join("\n");
  if (!focus) {
    return head;
  }
  return `${head}\n\nAdditional focus from the operator:\n${focus}`;
}

export function buildWorkingTreeReviewTask(userTask) {
  const focus = (userTask ?? "").trim();
  const head = [
    "Review the current uncommitted working tree (staged and unstaged changes,",
    "plus relevant untracked files). Focus on correctness bugs, security issues,",
    "regressions, and incomplete work.",
  ].join("\n");
  if (!focus) {
    return head;
  }
  return `${head}\n\nAdditional focus from the operator:\n${focus}`;
}

export function buildAdversarialTask(userTask) {
  const focus = (userTask ?? "").trim();
  return [
    "You are an ADVERSARIAL reviewer. Do not compliment the design. Attack it.",
    "Prioritize: wrong abstractions, silent data loss, auth/authz holes, race",
    "conditions, failure modes, operational risk, and simpler safer alternatives.",
    "For each finding: severity (critical/high/medium/low), concrete reproduction",
    "or proof sketch, and a brief fix hint. Rank findings by severity.",
    "If nothing serious is wrong, say so explicitly and still name residual risks.",
    "Use live web search when it strengthens an attack (current CVEs, API docs,",
    "breaking changes). End the answer with a machine-parseable Sources block:",
    "Sources:",
    "- https://example.com/page | Title | what this source grounded",
    "One bullet per source; real URLs only.",
    focus ? `\nOperator focus:\n${focus}` : "",
  ]
    .filter(Boolean)
    .join("\n");
}

export function resolvePathExists(p) {
  try {
    return fs.existsSync(p);
  } catch {
    return false;
  }
}

export function joinCwd(cwd, rel) {
  return path.isAbsolute(rel) ? rel : path.resolve(cwd, rel);
}
