// plugin/scripts/lib/deny-write.mjs
//
// Protected write-deny globs + match SSOT for the shared auto/peer apply
// pre-block. Data: plugin/references/deny-write-globs.json (parity with Python
// groklib.deny_write / direct_finalize.path_matches_deny). No second list.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const DENY_WRITE_SSOT_PATH = path.resolve(
  HERE,
  "../../references/deny-write-globs.json"
);

let _globs = null;
let _doc = null;

/**
 * Load and cache the deny-write SSOT JSON. Fail closed if missing/invalid.
 * @returns {{schemaVersion: number, globs: string[], matchVectors?: object[]}}
 */
export function loadDenyWriteSsot() {
  if (_doc) return _doc;
  if (!fs.existsSync(DENY_WRITE_SSOT_PATH)) {
    throw new Error(`deny-write SSOT missing at ${DENY_WRITE_SSOT_PATH}`);
  }
  const raw = fs.readFileSync(DENY_WRITE_SSOT_PATH, "utf8");
  const doc = JSON.parse(raw);
  if (!Array.isArray(doc.globs) || doc.globs.length === 0) {
    throw new Error("deny-write SSOT has empty/invalid globs");
  }
  if (!doc.globs.every((g) => typeof g === "string" && g)) {
    throw new Error("deny-write SSOT globs must be non-empty strings");
  }
  _doc = doc;
  _globs = doc.globs.slice();
  return _doc;
}

/**
 * @returns {string[]}
 */
export function denyWriteGlobs() {
  loadDenyWriteSsot();
  return _globs.slice();
}

/** Compat export (tuple-like array) for tests mirroring Python DENY_WRITE_GLOBS. */
export function getDenyWriteGlobs() {
  return denyWriteGlobs();
}

/**
 * POSIX-normalize a repo-relative path WITHOUT stripping a leading dotfile.
 * Mirrors Python groklib.deny_write.posix_rel.
 * @param {string} p
 * @returns {string}
 */
export function posixRel(p) {
  let norm = String(p || "").replace(/\\/g, "/");
  while (norm.startsWith("./")) norm = norm.slice(2);
  return norm;
}

/**
 * Python-fnmatch-compatible matcher for the patterns we ship (literals + *).
 * ``*`` matches any string including ``/`` (Python stdlib fnmatch parity).
 * @param {string} name
 * @param {string} pattern
 * @returns {boolean}
 */
export function fnmatchStar(name, pattern) {
  let out = "^";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === "*") {
      out += ".*";
      continue;
    }
    if (c === "?") {
      out += ".";
      continue;
    }
    if ("\\.^$+{}()|[]".includes(c)) out += `\\${c}`;
    else out += c;
  }
  out += "$";
  return new RegExp(out, "s").test(name);
}

/**
 * True when a repo-relative path matches the deny-write globs or any path
 * component is `.git` (root, nested vendor/lib/.git, modules). Same algorithm
 * as Python path_matches_deny / golden vectors.
 * @param {string} pathStr
 * @param {string[]} [globs]
 * @returns {boolean}
 */
export function pathMatchesDeny(pathStr, globs) {
  const patterns = globs == null ? denyWriteGlobs() : globs;
  const norm = posixRel(pathStr);
  if (!norm) return false;
  const parts = norm.split("/").filter(Boolean);
  // Any component named .git (root, nested vendor repo, submodule gitdir).
  if (parts.includes(".git")) return true;
  const base = parts[parts.length - 1] || "";
  for (const pattern of patterns) {
    if (fnmatchStar(norm, pattern) || fnmatchStar(base, pattern)) return true;
  }
  return false;
}

/**
 * First deny-matched path from a touch set (sorted for stable reporting), or null.
 * @param {Iterable<string>} paths
 * @returns {string[]}
 */
export function protectedPathsIn(paths) {
  const offenders = [];
  for (const p of paths) {
    if (pathMatchesDeny(p)) offenders.push(p);
  }
  return offenders.sort();
}
