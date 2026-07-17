// plugin/scripts/lib/jobs.mjs
//
// Per-workspace job registry for Grok companion runs (status / result / cancel).
// Mirrors the codex-plugin job idea without depending on Codex. Plugin-local
// state only; safety still lives in the wrapper (hardened mode).

import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { resolveWorkspaceRoot } from "./gate-state.mjs";
import {
  isNotificationMode,
  NOTIFICATION_MODES,
  parseNotificationMode,
  parseWebhookUrl,
} from "./notification-modes.mjs";

export { isNotificationMode, NOTIFICATION_MODES, parseNotificationMode, parseWebhookUrl };

const PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA";
const FALLBACK = path.join(os.tmpdir(), "grok-companion");
const MAX_JOBS = 50;
const JOB_ID_RE = /^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$/;
const DIR_MODE = 0o700;
const FILE_MODE = 0o600;

/** Single source of jobs-index config defaults (design §11). */
export const DEFAULT_JOBS_CONFIG = Object.freeze({
  runMode: "hardened",
  notificationMode: "off",
  notificationWebhookUrl: null,
  lastRescueJobId: null,
});

/** Normalize stored/corrupt config values to a known mode (default off). */
function normalizeNotificationMode(value) {
  return parseNotificationMode(value) ?? DEFAULT_JOBS_CONFIG.notificationMode;
}

/** Stored corrupt webhook URLs fall back to null. */
function normalizeWebhookUrl(value) {
  const parsed = parseWebhookUrl(value);
  return parsed.ok ? parsed.url : null;
}

function normalizeConfig(raw) {
  return {
    runMode: raw?.runMode === "direct" ? "direct" : "hardened",
    notificationMode: normalizeNotificationMode(raw?.notificationMode),
    notificationWebhookUrl: normalizeWebhookUrl(raw?.notificationWebhookUrl),
    lastRescueJobId: raw?.lastRescueJobId ?? null,
  };
}

export function isValidJobId(jobId) {
  return typeof jobId === "string" && JOB_ID_RE.test(jobId);
}

function mkdirPrivate(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: DIR_MODE });
  try {
    fs.chmodSync(dir, DIR_MODE);
  } catch {
    /* best-effort on platforms without chmod */
  }
}

function writePrivate(filePath, content) {
  fs.writeFileSync(filePath, content, { encoding: "utf8", mode: FILE_MODE });
  try {
    fs.chmodSync(filePath, FILE_MODE);
  } catch {
    /* best-effort */
  }
}

function assertJobIdSafe(jobId) {
  if (!isValidJobId(jobId)) {
    throw new Error(`invalid job id: ${jobId}`);
  }
  return jobId;
}

function nowIso() {
  return new Date().toISOString();
}

function stateRoot(cwd, env = process.env) {
  const workspaceRoot = resolveWorkspaceRoot(cwd);
  let canonical = workspaceRoot;
  try {
    canonical = fs.realpathSync.native(workspaceRoot);
  } catch {
    canonical = workspaceRoot;
  }
  const slug =
    (path.basename(workspaceRoot) || "workspace")
      .replace(/[^a-zA-Z0-9._-]+/g, "-")
      .replace(/^-+|-+$/g, "") || "workspace";
  const hash = createHash("sha256").update(canonical).digest("hex").slice(0, 16);
  const pluginData = (env[PLUGIN_DATA_ENV] ?? env.PLUGIN_DATA ?? "").trim();
  const root = pluginData ? path.join(pluginData, "state") : FALLBACK;
  return path.join(root, `${slug}-${hash}`);
}

export function jobsDir(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), "jobs");
}

function indexPath(cwd, env = process.env) {
  return path.join(stateRoot(cwd, env), "jobs-index.json");
}

function ensure(cwd, env = process.env) {
  mkdirPrivate(stateRoot(cwd, env));
  mkdirPrivate(jobsDir(cwd, env));
}

function loadIndex(cwd, env = process.env) {
  ensure(cwd, env);
  const file = indexPath(cwd, env);
  if (!fs.existsSync(file)) {
    return { version: 1, jobs: [], config: { ...DEFAULT_JOBS_CONFIG } };
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    return {
      version: 1,
      jobs: Array.isArray(parsed.jobs) ? parsed.jobs : [],
      config: normalizeConfig(parsed.config),
    };
  } catch {
    return { version: 1, jobs: [], config: { ...DEFAULT_JOBS_CONFIG } };
  }
}

function saveIndex(cwd, index, env = process.env) {
  ensure(cwd, env);
  const jobs = [...(index.jobs ?? [])]
    .sort((a, b) => String(b.updatedAt ?? "").localeCompare(String(a.updatedAt ?? "")))
    .slice(0, MAX_JOBS);
  const config = normalizeConfig(index.config);
  const payload = {
    version: 1,
    config: {
      runMode: config.runMode,
      notificationMode: config.notificationMode,
      notificationWebhookUrl: config.notificationWebhookUrl,
      lastRescueJobId: config.lastRescueJobId,
    },
    jobs,
  };
  writePrivate(indexPath(cwd, env), `${JSON.stringify(payload, null, 2)}\n`);
  return payload;
}

export function getRunMode(cwd, env = process.env) {
  const fromEnv = (env.GROK_SKILLS_MODE ?? "").trim().toLowerCase();
  if (fromEnv === "direct" || fromEnv === "hardened") {
    return fromEnv;
  }
  return loadIndex(cwd, env).config.runMode === "direct" ? "direct" : "hardened";
}

export function setRunMode(cwd, mode, env = process.env) {
  const index = loadIndex(cwd, env);
  index.config.runMode = mode === "direct" ? "direct" : "hardened";
  saveIndex(cwd, index, env);
  return index.config.runMode;
}

/**
 * @returns {{ notificationMode: string, notificationWebhookUrl: string|null }}
 */
export function getNotificationConfig(cwd, env = process.env) {
  const config = loadIndex(cwd, env).config;
  return {
    notificationMode: config.notificationMode,
    notificationWebhookUrl: config.notificationWebhookUrl,
  };
}

/**
 * @param {string} cwd
 * @param {{ notificationMode?: string, notificationWebhookUrl?: string|null }} patch
 */
export function setNotificationConfig(cwd, patch, env = process.env) {
  const index = loadIndex(cwd, env);
  if (patch.notificationMode !== undefined) {
    // Invalid modes leave prior prefs unchanged (never clobber auto -> off).
    const mode = parseNotificationMode(patch.notificationMode);
    if (mode) {
      index.config.notificationMode = mode;
    }
  }
  if (patch.notificationWebhookUrl !== undefined) {
    // Invalid non-empty URLs leave prior webhook unchanged; empty clears.
    const parsed = parseWebhookUrl(patch.notificationWebhookUrl);
    if (parsed.ok) {
      index.config.notificationWebhookUrl = parsed.url;
    }
  }
  saveIndex(cwd, index, env);
  return getNotificationConfig(cwd, env);
}

export function mintJobId() {
  const ts = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  return `${ts}-${randomBytes(3).toString("hex")}`;
}

export function jobPaths(cwd, jobId, env = process.env) {
  assertJobIdSafe(jobId);
  const root = jobsDir(cwd, env);
  const dir = path.resolve(root, jobId);
  if (!dir.startsWith(root + path.sep) && dir !== root) {
    throw new Error(`job path escapes jobs dir: ${jobId}`);
  }
  return {
    dir,
    meta: path.join(dir, "job.json"),
    log: path.join(dir, "job.log"),
    stdout: path.join(dir, "stdout.json"),
  };
}

export function createJob(cwd, partial, env = process.env) {
  const id = partial.id || mintJobId();
  const paths = jobPaths(cwd, id, env);
  mkdirPrivate(paths.dir);
  const job = {
    id,
    kind: partial.kind || "run",
    mode: partial.mode || null,
    status: "running",
    runMode: partial.runMode || getRunMode(cwd, env),
    pid: partial.pid ?? null,
    pgid: partial.pgid ?? null,
    runId: partial.runId ?? null,
    createdAt: nowIso(),
    updatedAt: nowIso(),
    summary: partial.summary ?? null,
    error: null,
  };
  writePrivate(paths.meta, `${JSON.stringify(job, null, 2)}\n`);
  writePrivate(paths.log, `[${job.createdAt}] start ${job.kind} mode=${job.mode}\n`);
  const index = loadIndex(cwd, env);
  index.jobs = [job, ...index.jobs.filter((j) => j.id !== id)];
  if (job.kind === "rescue") {
    index.config.lastRescueJobId = id;
  }
  saveIndex(cwd, index, env);
  return job;
}

export function updateJob(cwd, jobId, patch, env = process.env) {
  const paths = jobPaths(cwd, jobId, env);
  let job = { id: jobId };
  if (fs.existsSync(paths.meta)) {
    try {
      job = JSON.parse(fs.readFileSync(paths.meta, "utf8"));
    } catch {
      job = { id: jobId };
    }
  }
  job = { ...job, ...patch, id: jobId, updatedAt: nowIso() };
  mkdirPrivate(paths.dir);
  writePrivate(paths.meta, `${JSON.stringify(job, null, 2)}\n`);
  const index = loadIndex(cwd, env);
  index.jobs = [job, ...index.jobs.filter((j) => j.id !== jobId)];
  saveIndex(cwd, index, env);
  return job;
}

export function appendJobLog(cwd, jobId, line, env = process.env) {
  const paths = jobPaths(cwd, jobId, env);
  mkdirPrivate(paths.dir);
  fs.appendFileSync(paths.log, `[${nowIso()}] ${line}\n`, { encoding: "utf8", mode: FILE_MODE });
}

export function storeJobStdout(cwd, jobId, text, env = process.env) {
  const paths = jobPaths(cwd, jobId, env);
  mkdirPrivate(paths.dir);
  writePrivate(paths.stdout, text);
}

export function listJobs(cwd, env = process.env) {
  return loadIndex(cwd, env).jobs;
}

export function getJob(cwd, jobId, env = process.env) {
  if (!jobId) {
    const jobs = listJobs(cwd, env);
    return jobs[0] ?? null;
  }
  if (!isValidJobId(jobId)) {
    return null;
  }
  const paths = jobPaths(cwd, jobId, env);
  if (fs.existsSync(paths.meta)) {
    try {
      return JSON.parse(fs.readFileSync(paths.meta, "utf8"));
    } catch {
      return null;
    }
  }
  return listJobs(cwd, env).find((j) => j.id === jobId) ?? null;
}

/**
 * Resolve a job by its stored wrapper/direct runId (newest-first index order).
 * @returns {object|null}
 */
export function findJobByRunId(cwd, runId, env = process.env) {
  if (!runId) return null;
  const jobs = listJobs(cwd, env); // newest-first ordering already used by the table
  return jobs.find((j) => j.runId === runId) || null;
}

/**
 * Resolve a job from a positional that may be a job id or a runId.
 * Same id shape (JOB_ID_RE / RUN_ID_RE); exact job-id match wins, then runId.
 * Collision: job A id === job B runId returns A (getJob), not B.
 * @returns {object|null}
 */
export function resolveJobByIdOrRunId(cwd, idOrRunId, env = process.env) {
  // Prefer exact job-id match so a job id that collides with another job's
  // runId never resolves to the wrong record (shared YYYYMMDDTHHMMSSZ shape).
  let job = getJob(cwd, idOrRunId, env);
  if (!job && idOrRunId) {
    job = findJobByRunId(cwd, idOrRunId, env);
  }
  return job;
}

export function readJobStdout(cwd, jobId, env = process.env) {
  const paths = jobPaths(cwd, jobId, env);
  if (!fs.existsSync(paths.stdout)) {
    return null;
  }
  return fs.readFileSync(paths.stdout, "utf8");
}

export function getLastRescueJobId(cwd, env = process.env) {
  return loadIndex(cwd, env).config.lastRescueJobId ?? null;
}

export function formatJobsTable(jobs) {
  if (!jobs.length) {
    return "No Grok jobs recorded for this workspace yet.\n";
  }
  const header = ["ID", "KIND", "STATUS", "MODE", "RUN", "UPDATED"];
  const rows = jobs.map((j) => [
    j.id,
    j.kind ?? "",
    j.status ?? "",
    j.runMode ?? "",
    j.runId ?? "",
    (j.updatedAt ?? "").replace("T", " ").replace("Z", ""),
  ]);
  const widths = header.map((h, i) => Math.max(h.length, ...rows.map((r) => String(r[i]).length)));
  const fmt = (cells) => cells.map((c, i) => String(c).padEnd(widths[i])).join("  ");
  return [fmt(header), fmt(widths.map((w) => "-".repeat(w))), ...rows.map(fmt)].join("\n") + "\n";
}
