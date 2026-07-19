// plugin/scripts/lib/integrate-paths.mjs
//
// Path decode + patch touch-set helpers for integrate apply (auto + peer).
// C-quote golden vectors: plugin/references/git-c-quoted-path-vectors.json
// (parity with Python groklib.git_path_quote). Do not apply C-unquote to
// NUL-safe -z path inventories (already raw). One token decoder is SSOT for
// both full-token unquote and mid-string parseCQuotedAt.

const C_QUOTE_NAMED = {
  a: 7,
  b: 8,
  t: 9,
  n: 10,
  v: 11,
  f: 12,
  r: 13,
  '"': 34,
  "\\": 92,
};

/**
 * Consume one C-quote unit at index i in s.
 * Returns [byte|null, nextIndex]. byte null means incomplete escape (caller
 * decides: full-token unquote no-ops trailing `\`; mid-token parse fails closed).
 * @param {string} s
 * @param {number} i
 * @returns {[number|null, number]}
 */
function consumeCQuoteUnit(s, i) {
  if (i >= s.length) return [null, i];
  if (s[i] !== "\\") return [s.charCodeAt(i) & 0xff, i + 1];
  const next = s[i + 1];
  if (next === undefined) return [null, i + 1]; // incomplete trailing `\`
  if (next >= "0" && next <= "7") {
    let j = i + 1;
    let oct = "";
    while (j < s.length && oct.length < 3 && s[j] >= "0" && s[j] <= "7") {
      oct += s[j];
      j += 1;
    }
    return [parseInt(oct, 8) & 0xff, j];
  }
  if (next in C_QUOTE_NAMED) return [C_QUOTE_NAMED[next], i + 2];
  return [s.charCodeAt(i + 1) & 0xff, i + 2];
}

/**
 * Decode C-quote escape sequences in a body (no surrounding quotes).
 * Trailing incomplete `\` is a no-op (golden trailing-backslash-noop).
 * @param {string} body
 * @returns {number[]} raw bytes
 */
function decodeCQuoteBodyBytes(body) {
  const bytes = [];
  let i = 0;
  while (i < body.length) {
    const [b, next] = consumeCQuoteUnit(body, i);
    if (b != null) bytes.push(b);
    i = next;
  }
  return bytes;
}

/**
 * Parse one C-quoted token at `start` (must be `"`). Returns [decoded, nextIndex]
 * or [null, start] on failure. Uses the same escape SSOT as unquoteGitPath.
 * @param {string} s
 * @param {number} start
 * @returns {[string|null, number]}
 */
function parseCQuotedAt(s, start) {
  if (start >= s.length || s[start] !== '"') return [null, start];
  let i = start + 1;
  const bytes = [];
  while (i < s.length) {
    if (s[i] === '"') {
      return [Buffer.from(bytes).toString("utf8"), i + 1];
    }
    if (s[i] === "\\" && s[i + 1] === undefined) return [null, start];
    const [b, next] = consumeCQuoteUnit(s, i);
    if (b == null) return [null, start];
    bytes.push(b);
    i = next;
  }
  return [null, start];
}

/**
 * Decode one git core.quotePath C-style token (`"..."` with `\\NNN` octal and
 * named escapes). Shared golden vectors: plugin/references/git-c-quoted-path-vectors.json
 * (parity with Python groklib.git_path_quote). Do **not** apply to NUL-safe
 * `-z` path_inventory payloads (already raw).
 *
 * @param {string} p
 * @returns {string}
 */
export function unquoteGitPath(p) {
  const s = String(p).trim();
  if (!(s.startsWith('"') && s.endsWith('"'))) return s;
  // Full-token path form: decode interior via the single escape SSOT. Prefer
  // balanced parse when possible; otherwise body decode (trailing-`\` golden).
  const [decoded, next] = parseCQuotedAt(s, 0);
  if (decoded != null && next === s.length) return decoded;
  return Buffer.from(decodeCQuoteBodyBytes(s.slice(1, -1))).toString("utf8");
}

/**
 * Both sides of a numstat rename form ("old => new", "{a => b}/f") - a dirty
 * `old` path being renamed carries the operator's edits into `new`, so the
 * dirty-overlap guard must consider both (review). Non-renames return [p].
 */
function renamePathSides(p) {
  const s = String(p);
  const collapse = (x) => x.replace(/\/{2,}/g, "/");
  const brace = s.match(/^(.*)\{([^}]*?) => ([^}]*?)\}(.*)$/);
  if (brace) {
    const [, pre, oldMid, newMid, post] = brace;
    return [collapse(pre + oldMid + post), collapse(pre + newMid + post)];
  }
  const idx = s.indexOf(" => ");
  if (idx >= 0) return [collapse(s.slice(0, idx)), collapse(s.slice(idx + 4))];
  return [collapse(s)];
}

/**
 * Repo-relative dirty paths from `git status --porcelain -z --untracked-files=all`.
 * @param {string} statusOutput
 * @returns {Set<string>}
 */
export function parseDirtyStatusPaths(statusOutput) {
  // Input is `git status --porcelain -z --untracked-files=all`: NUL-TERMINATED
  // entries with paths NOT quoted (no `"..."`, no ` -> ` arrow). A rename/copy
  // entry (R/C in either status column) is followed by a SECOND NUL-token holding
  // the paired path. We add BOTH the status-line path and any paired path
  // (direction-agnostic), so the overlap guard catches either name - and a literal
  // ` -> ` (even a quoted one) inside a filename can never be mis-split.
  const set = new Set();
  const entries = String(statusOutput || "").split("\0");
  for (let i = 0; i < entries.length; i++) {
    const raw = entries[i];
    if (!raw) continue; // trailing empty token after the final NUL
    const xy = raw.slice(0, 2);
    const p = raw.slice(3); // "XY " prefix: 2 status columns + 1 space
    if (p) set.add(p);
    if (xy[0] === "R" || xy[0] === "C" || xy[1] === "R" || xy[1] === "C") {
      i += 1;
      const paired = entries[i]; // the rename/copy source path (raw, no prefix)
      if (paired) set.add(paired);
    }
  }
  return set;
}

/**
 * Repo-relative paths a patch touches, from `git apply --numstat --binary`.
 * @param {string} numstatOutput
 * @returns {string[]}
 */
export function parseNumstatPaths(numstatOutput) {
  const paths = [];
  for (const raw of String(numstatOutput || "").split("\n")) {
    if (!raw.trim()) continue;
    const parts = raw.split("\t"); // "<added>\t<deleted>\t<path>"
    if (parts.length < 3) continue;
    const pathField = parts.slice(2).join("\t");
    const sides = renamePathSides(pathField);
    for (const side of sides) paths.push(unquoteGitPath(side));
    // If the field LOOKED like a rename (split changed it), also keep the raw
    // field: a real filename literally containing " => " / "{...}" (git does not
    // quote those) would be mis-split, so the raw path keeps the dirty-overlap
    // guard from failing open. No duplicate for ordinary paths.
    if (sides.length !== 1 || sides[0] !== pathField) {
      paths.push(unquoteGitPath(pathField));
    }
  }
  return paths;
}

/**
 * Strip git `a/` / `b/` prefix from a decoded `diff --git` path token.
 * @param {string} pathToken
 * @returns {string}
 */
function stripDiffGitAbPrefix(pathToken) {
  const s = String(pathToken || "");
  if (s.startsWith("a/") || s.startsWith("b/")) return s.slice(2);
  return s;
}

/**
 * Next `diff --git` path token (decoded, still with a/b prefix) and new index.
 * Parity with Python git_path_quote.next_diff_git_token.
 * @param {string} s
 * @param {number} i
 * @param {boolean} isFirst
 * @returns {[string|null, number]}
 */
function nextDiffGitToken(s, i, isFirst) {
  const n = s.length;
  while (i < n && (s[i] === " " || s[i] === "\t")) i += 1;
  if (i >= n) return [null, i];
  if (s[i] === '"') return parseCQuotedAt(s, i);
  if (isFirst) {
    const sepB = s.indexOf(" b/", i);
    if (sepB >= 0) return [s.slice(i, sepB), sepB];
    const sepQ = s.indexOf(' "', i);
    if (sepQ >= 0) return [s.slice(i, sepQ), sepQ];
    return [null, i];
  }
  return [s.slice(i).replace(/\s+$/, ""), n];
}

/**
 * Parse path pair after `diff --git ` into repo-relative paths (no a/b).
 * @param {string} rest
 * @returns {[string, string]|null}
 */
export function parseDiffGitHeaderPaths(rest) {
  const [aRaw, i] = nextDiffGitToken(rest, 0, true);
  if (aRaw == null) return null;
  const [bRaw] = nextDiffGitToken(rest, i, false);
  if (bRaw == null) return null;
  return [stripDiffGitAbPrefix(aRaw), stripDiffGitAbPrefix(bRaw)];
}

/**
 * Repo-relative touch set from patch `diff --git` headers and rename/copy
 * from/to lines. Pure renames report destination-only via numstat; headers keep
 * the dirty-overlap guard from failing open on a dirty rename SOURCE.
 * Uses the same C-quote decode as unquoteGitPath / golden vectors.
 * @param {string|Buffer} patchBytes
 * @returns {Set<string>}
 */
export function pathsFromGitPatch(patchBytes) {
  const text = Buffer.isBuffer(patchBytes)
    ? patchBytes.toString("utf8")
    : String(patchBytes || "");
  const found = new Set();
  const add = (p) => {
    if (typeof p === "string" && p && p !== "/dev/null") found.add(p);
  };
  for (const rawLine of text.split(/\r?\n/)) {
    if (rawLine.startsWith("diff --git ")) {
      const pair = parseDiffGitHeaderPaths(rawLine.slice("diff --git ".length));
      if (pair) {
        add(pair[0]);
        add(pair[1]);
      }
      continue;
    }
    for (const prefix of ["rename from ", "rename to ", "copy from ", "copy to "]) {
      if (rawLine.startsWith(prefix)) {
        add(unquoteGitPath(rawLine.slice(prefix.length).trim()));
        break;
      }
    }
  }
  return found;
}
