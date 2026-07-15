// plugin/scripts/lib/session-stamp.mjs
//
// Workspace-keyed session stamps for /grok:transfer (SessionStart hook).
// Prefer CLAUDE_PLUGIN_DATA / XDG state; fall back to private /tmp path.
// Never a single global latest.json for all projects.

import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { resolveWorkspaceRoot } from "./gate-state.mjs";

const DIR_MODE = 0o700;
const FILE_MODE = 0o600;
const MAX_TRANSFER_BYTES = 2 * 1024 * 1024; // 2 MiB
const MAX_TRANSFER_LINES = 80;
const MAX_SNIPPET_CHARS = 4000;

function workspaceKey(cwd) {
  const root = resolveWorkspaceRoot(cwd);
  let canonical = root;
  try {
    canonical = fs.realpathSync.native(root);
  } catch {
    canonical = root;
  }
  return createHash("sha256").update(canonical).digest("hex").slice(0, 16);
}

function stampRoot(env = process.env) {
  const pluginData = (env.CLAUDE_PLUGIN_DATA ?? env.PLUGIN_DATA ?? "").trim();
  if (pluginData) {
    return path.join(pluginData, "session-stamps");
  }
  const xdg = (env.XDG_STATE_HOME ?? "").trim();
  if (xdg && path.isAbsolute(xdg)) {
    return path.join(xdg, "grok-skills", "session-stamps");
  }
  return path.join(os.tmpdir(), "grok-companion-session");
}

export function sessionStampPath(cwd, env = process.env) {
  return path.join(stampRoot(env), `${workspaceKey(cwd)}.json`);
}

function mkdirPrivate(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: DIR_MODE });
  try {
    fs.chmodSync(dir, DIR_MODE);
  } catch {
    /* best-effort */
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

/**
 * Write SessionStart stamp for this workspace. Returns stamp path.
 */
export function writeSessionStamp(cwd, payload, env = process.env) {
  const file = sessionStampPath(cwd, env);
  mkdirPrivate(path.dirname(file));
  writePrivate(file, `${JSON.stringify({ ...payload, cwd: cwd || payload.cwd }, null, 2)}\n`);
  return file;
}

/**
 * Read stamp for this workspace (or null).
 */
export function readSessionStamp(cwd, env = process.env) {
  const file = sessionStampPath(cwd, env);
  if (!fs.existsSync(file)) {
    return null;
  }
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

/**
 * Allowlisted transcript roots (Claude/Codex-style session dirs under home).
 * Absolute --source must resolve under one of these unless force=true.
 */
export function allowedTranscriptRoots(env = process.env) {
  const home = os.homedir();
  const roots = [
    path.join(home, ".claude"),
    path.join(home, ".config", "claude"),
    path.join(home, ".codex"),
    path.join(home, "Library", "Application Support", "Claude"),
    path.join(home, "Library", "Application Support", "Codex"),
  ];
  const extra = (env.GROK_TRANSFER_ALLOW_ROOTS ?? "").trim();
  if (extra) {
    for (const part of extra.split(path.delimiter)) {
      if (part.trim()) roots.push(path.resolve(part.trim()));
    }
  }
  return roots;
}

function isUnderRoot(candidate, root) {
  const c = path.resolve(candidate);
  const r = path.resolve(root);
  return c === r || c.startsWith(r + path.sep);
}

/**
 * Validate a transfer source path. Returns { ok, path, reason }.
 */
export function resolveTransferSource(sourcePath, { force = false, env = process.env } = {}) {
  if (!sourcePath || typeof sourcePath !== "string") {
    return { ok: false, path: null, reason: "missing source path" };
  }
  let resolved;
  try {
    resolved = fs.realpathSync(path.resolve(sourcePath));
  } catch (err) {
    return { ok: false, path: null, reason: `cannot resolve source: ${err.message}` };
  }
  let st;
  try {
    st = fs.statSync(resolved);
  } catch (err) {
    return { ok: false, path: null, reason: `cannot stat source: ${err.message}` };
  }
  if (!st.isFile()) {
    return { ok: false, path: null, reason: "source is not a regular file" };
  }
  if (st.size > MAX_TRANSFER_BYTES) {
    return {
      ok: false,
      path: null,
      reason: `source exceeds ${MAX_TRANSFER_BYTES} bytes (got ${st.size})`,
    };
  }
  if (!force) {
    const roots = allowedTranscriptRoots(env);
    const allowed = roots.some((root) => {
      try {
        return isUnderRoot(resolved, fs.existsSync(root) ? fs.realpathSync(root) : root);
      } catch {
        return isUnderRoot(resolved, root);
      }
    });
    if (!allowed) {
      return {
        ok: false,
        path: null,
        reason:
          "source is outside allowed transcript roots (~/.claude, ~/.codex, …). " +
          "Pass --force to override, or set GROK_TRANSFER_ALLOW_ROOTS.",
      };
    }
  }
  if (!force) {
    const lower = resolved.toLowerCase();
    if (!lower.endsWith(".jsonl") && !lower.endsWith(".json")) {
      return {
        ok: false,
        path: null,
        reason: "source must be a .jsonl or .json session file (or pass --force)",
      };
    }
  }
  return { ok: true, path: resolved, reason: null };
}

/**
 * Build transfer task text from a session file (capped).
 */
export function buildTransferTaskBody(sessionPath) {
  const raw = fs.readFileSync(sessionPath, "utf8");
  if (Buffer.byteLength(raw, "utf8") > MAX_TRANSFER_BYTES) {
    throw new Error(`session file exceeds ${MAX_TRANSFER_BYTES} bytes`);
  }
  const lines = raw.split("\n").filter(Boolean).slice(-MAX_TRANSFER_LINES);
  const snippets = [];
  for (const line of lines) {
    try {
      const obj = JSON.parse(line);
      const role = obj.role || obj.type || "";
      const content =
        typeof obj.message?.content === "string"
          ? obj.message.content
          : typeof obj.content === "string"
            ? obj.content
            : Array.isArray(obj.message?.content)
              ? obj.message.content.map((c) => c.text || "").join("\n")
              : "";
      if (content && content.trim()) {
        snippets.push(`[${role}] ${content.trim().slice(0, MAX_SNIPPET_CHARS)}`);
      }
    } catch {
      // skip non-jsonl lines
    }
  }
  return [
    "You are continuing work transferred from a Claude Code session.",
    "Use the transcript excerpts below as context. Prefer verifying claims against the repo.",
    "",
    "## Transcript excerpts",
    snippets.join("\n\n") || "(empty)",
    "",
    "## Your job",
    "Summarize the state of work, list open risks, and propose the next concrete steps.",
  ].join("\n");
}

export function writeTransferPack(body, env = process.env) {
  const base =
    (env.CLAUDE_PLUGIN_DATA ?? env.PLUGIN_DATA ?? "").trim() ||
    path.join(os.tmpdir(), "grok-transfer");
  mkdirPrivate(base);
  const outDir = fs.mkdtempSync(path.join(base, "pack-"));
  try {
    fs.chmodSync(outDir, DIR_MODE);
  } catch {
    /* best-effort */
  }
  const taskPath = path.join(outDir, "transfer-task.md");
  writePrivate(taskPath, body);
  return taskPath;
}

export const TRANSFER_LIMITS = {
  MAX_TRANSFER_BYTES,
  MAX_TRANSFER_LINES,
  MAX_SNIPPET_CHARS,
};
