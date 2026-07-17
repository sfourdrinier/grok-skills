// plugin/scripts/lib/integrate.mjs
//
// Apply-on-verified-ready for integration=auto (Task 7.3).
// Apply-time revalidation (review guard 5): re-run handoff, locate patch,
// git apply --check, then apply. Never half-apply; reverse on mid-apply failure.

import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { runsDirFor, safeRunIdForRunsDir } from "../progress-relay.mjs";

/**
 * Resolve runs/<runId>/artifacts/implementation.patch under the wrapper state
 * root (same XDG layout handoff uses). Null when missing or runId unsafe.
 *
 * @param {string} runId
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string|null}
 */
export function locateImplementationPatch(runId, env = process.env) {
  const runsDir = runsDirFor(env);
  const safe = safeRunIdForRunsDir(runId, runsDir);
  if (!safe) return null;
  const patchPath = path.join(runsDir, safe, "artifacts", "implementation.patch");
  try {
    const st = fs.statSync(patchPath);
    if (!st.isFile() || st.size <= 0) return null;
    return patchPath;
  } catch {
    return null;
  }
}

/**
 * @param {string} filePath
 * @returns {string}
 */
export function sha256File(filePath) {
  const h = createHash("sha256");
  h.update(fs.readFileSync(filePath));
  return h.digest("hex");
}

function git(cwd, args) {
  const result = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  return {
    code: typeof result.status === "number" ? result.status : 1,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
    error: result.error || null,
  };
}

/**
 * Apply-time revalidation + git apply to the operator target tree.
 * Caller supplies runHandoff (typically runHandoffCaptured) to avoid import cycles.
 *
 * @param {object} opts
 * @param {string} opts.wrapper
 * @param {string} opts.runId
 * @param {string} opts.targetRepo absolute target workspace root
 * @param {(wrapper: string, args: string[], opts?: object) => { code: number, envelope: object|null }} opts.runHandoff
 * @param {(line: string) => void} [opts.stderrLine]
 * @param {NodeJS.ProcessEnv} [opts.env]
 * @returns {{
 *   ok: boolean,
 *   outcome: string,
 *   reason?: string,
 *   runId: string,
 *   patchPath?: string,
 *   patchSha?: string,
 *   preStatus?: string,
 *   postStatus?: string,
 * }}
 */
export function applyVerifiedPatch({
  wrapper,
  runId,
  targetRepo,
  runHandoff,
  stderrLine = (line) => process.stderr.write(`${line}\n`),
  env = process.env,
} = {}) {
  if (typeof runHandoff !== "function") {
    return {
      ok: false,
      outcome: "blocked-internal",
      reason: "runHandoff callback required",
      runId: runId || "",
    };
  }
  if (!runId || !targetRepo) {
    return {
      ok: false,
      outcome: "blocked-internal",
      reason: "runId and targetRepo required",
      runId: runId || "",
    };
  }

  // 1. Fresh dual-condition re-read (do not trust the earlier handoff read).
  stderrLine(`[grok-auto] apply-time revalidation for ${runId}`);
  const { code: hCode, envelope: hEnv } = runHandoff(wrapper, [
    "handoff",
    "--run-id",
    runId,
  ]);
  const ready = hEnv?.response?.integration?.ready === true;
  if (hCode !== 0 || !ready) {
    stderrLine(
      `[grok-auto] BLOCKED: apply-time handoff revalidation not ready for ${runId} ` +
        `(handoff exit ${hCode}, ready=${ready})`
    );
    return {
      ok: false,
      outcome: "blocked-revalidation",
      reason: "apply-time handoff revalidation not ready",
      runId,
    };
  }

  // 2. Locate patch under wrapper state root.
  const patchPath = locateImplementationPatch(runId, env);
  if (!patchPath) {
    stderrLine(
      `[grok-auto] BLOCKED: cannot locate implementation.patch for ${runId} ` +
        `under ${runsDirFor(env)}`
    );
    return {
      ok: false,
      outcome: "blocked-patch-missing",
      reason: "implementation.patch not found under state root",
      runId,
    };
  }

  const patchSha = sha256File(patchPath);
  const preStatus = git(targetRepo, ["status", "--short", "--untracked-files=all"]);

  // 3. Precondition: git apply --check --binary (tree may have moved since run).
  const check = git(targetRepo, ["apply", "--check", "--binary", patchPath]);
  if (check.code !== 0) {
    stderrLine(
      `[grok-auto] BLOCKED: git apply --check failed (target tree moved since run). ` +
        `PARTIAL/blocked - no apply attempted. pre-status:\n${preStatus.stdout || "(clean)"}`
    );
    if (check.stderr) stderrLine(check.stderr.trimEnd());
    return {
      ok: false,
      outcome: "blocked-apply-check",
      reason: "git apply --check failed; target tree incompatible with patch",
      runId,
      patchPath,
      patchSha,
      preStatus: preStatus.stdout,
      checkStderr: check.stderr,
    };
  }

  // 4. Apply. On failure mid-apply, attempt reverse to restore.
  const apply = git(targetRepo, ["apply", "--binary", patchPath]);
  if (apply.code !== 0) {
    stderrLine(`[grok-auto] apply failed; attempting reverse (git apply -R) to restore`);
    if (apply.stderr) stderrLine(apply.stderr.trimEnd());
    const rev = git(targetRepo, ["apply", "-R", "--binary", patchPath]);
    if (rev.code === 0) {
      stderrLine(`[grok-auto] rolled-back via git apply -R; target restored`);
      return {
        ok: false,
        outcome: "rolled-back",
        reason: "git apply failed; reverse succeeded",
        runId,
        patchPath,
        patchSha,
      };
    }
    stderrLine(
      `[grok-auto] reverse also failed; MANUAL-NEEDED - inspect target tree for partial apply`
    );
    return {
      ok: false,
      outcome: "manual-needed",
      reason: "git apply failed and reverse failed",
      runId,
      patchPath,
      patchSha,
    };
  }

  const postStatus = git(targetRepo, ["status", "--short", "--untracked-files=all"]);
  stderrLine(
    `[grok-auto] APPLIED runId=${runId} patchSha=${patchSha}\n` +
      `pre-status:\n${preStatus.stdout || "(clean)"}\n` +
      `post-status:\n${postStatus.stdout || "(clean)"}`
  );
  return {
    ok: true,
    outcome: "applied",
    runId,
    patchPath,
    patchSha,
    preStatus: preStatus.stdout,
    postStatus: postStatus.stdout,
  };
}

// --- Peer-stop integration (Task 7.4, extracted from grok-companion.mjs to
// keep the companion under the 900-line cap; reuses applyVerifiedPatch). ---
import { tryParseEnvelope } from "./render.mjs";
import { sanitizeRunId } from "./companion-terminal-notify.mjs";
import {
  parseIntegrationMode,
  getIntegrationMode,
  getIntegrationConsent,
  formatDirectIntegrationConsentMsg,
} from "./jobs.mjs";
import { resolveTargetWorkspaceRoot, parseTargetFlag } from "./git-context.mjs";

/** ACP default; GROK_DISABLE_ACP=1 opt-out (Task 7.4). */
export function isAcpDisabled(env = process.env) {
  const f = String(env.GROK_DISABLE_ACP ?? "").trim().toLowerCase();
  return f === "1" || f === "true" || f === "yes" || f === "on";
}

/**
 * On a READY peer-stop: auto/direct apply the verified patch to the target
 * tree (reusing applyVerifiedPatch's revalidation); review/worktree retain it.
 * @param {(line: string) => void} stderrLine
 */
export function maybeIntegratePeerStop(stdout, cwd, integrationFlag, rest, stderrLine) {
  const env = tryParseEnvelope(stdout || "");
  const ready =
    env?.response?.peer?.integrationReady === true ||
    env?.response?.integration?.ready === true;
  if (!ready || env?.status !== "success") return;
  const tArg = parseTargetFlag(rest) || env?.targetWorkspace || ".";
  const tWs = resolveTargetWorkspaceRoot(cwd, tArg);
  const mode =
    integrationFlag != null && String(integrationFlag).trim() !== ""
      ? parseIntegrationMode(integrationFlag)
      : getIntegrationMode(tWs);
  if (!mode) {
    stderrLine(
      `[grok-companion] invalid --integration ${JSON.stringify(integrationFlag)} ` +
        `(valid: direct|worktree|auto|review)`
    );
    return;
  }
  if (mode === "worktree" || mode === "review") {
    stderrLine(`[grok-peer] integration=${mode}: patch retained; not applied`);
    return;
  }
  if (mode === "direct" && !getIntegrationConsent(tWs)) {
    stderrLine(formatDirectIntegrationConsentMsg({ targetWorkspace: tWs, companionCwd: cwd }));
    return;
  }
  const runId = sanitizeRunId(env?.runId);
  const repo = env?.repository;
  if (!runId || typeof repo !== "string" || !repo) {
    stderrLine("[grok-peer] missing runId or repository on peer-stop envelope");
    return;
  }
  const patchPath = locateImplementationPatch(runId);
  if (!patchPath) {
    stderrLine(`[grok-peer] patch missing for run ${runId}`);
    return;
  }
  // Peer-stop already ran real validation and produced a ready manifest, so we
  // do not re-run handoff (unlike auto-code's applyVerifiedPatch). We still
  // guard the apply with git apply --check (TOCTOU: the tree may have moved).
  const git = (a) => spawnSync("git", ["-C", repo, ...a], { encoding: "utf8" });
  const check = git(["apply", "--check", "--binary", patchPath]);
  if (check.status !== 0) {
    stderrLine(`[grok-peer] git apply --check failed: ${(check.stderr || check.stdout || "").trim()}`);
    return;
  }
  const apply = git(["apply", "--binary", patchPath]);
  if (apply.status !== 0) {
    const detail = (apply.stderr || apply.stdout || "").trim();
    // Never leave a half-applied tree: reverse (git apply -R) like the auto path.
    const rev = git(["apply", "-R", "--binary", patchPath]);
    if (rev.status === 0) {
      stderrLine(`[grok-peer] git apply failed; rolled back via -R: ${detail}`);
    } else {
      stderrLine(
        `[grok-peer] git apply failed AND reverse failed; MANUAL-NEEDED ` +
          `(inspect ${repo} for partial apply): ${detail}`
      );
    }
    return;
  }
  stderrLine(`[grok-peer] applied ${patchPath} to ${repo}`);
}
