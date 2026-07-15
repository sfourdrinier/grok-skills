// plugin/scripts/lib/git-context.mjs
// Resolve default review targets from git (working tree / branch base).

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

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
