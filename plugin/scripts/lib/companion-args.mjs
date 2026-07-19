// plugin/scripts/lib/companion-args.mjs
//
// Pure argv flag-stripping for the companion entrypoint (extracted to keep
// grok-companion.mjs under the 900-line cap). Also owns the single last-wins
// split-or-equals value parser used by direct-mode and task-file extraction
// (parity with the wrapper's argparse and parseTargetFlag).

/** True if `args` contains `name` in split (`--x`) OR equals (`--x=v`) form.
 *  Presence check parity with the wrapper's argparse (which accepts both). */
export function hasFlagOrEquals(args, name) {
  const eq = name + "=";
  return Array.isArray(args) && args.some((a) => a === name || (typeof a === "string" && a.startsWith(eq)));
}

/**
 * Last-wins value for `--name value` or `--name=value` (argparse parity).
 * Does not consume a following flag as the value. Returns null when absent.
 * @param {string[]|null|undefined} args
 * @param {string} name e.g. "--task"
 * @returns {string|null}
 */
export function flagValue(args, name) {
  if (!Array.isArray(args) || typeof name !== "string" || !name) return null;
  const eq = name + "=";
  let val = null;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === name && args[i + 1] !== undefined) {
      // Do not consume a following FLAG as the value (parity with parseTargetFlag).
      val = String(args[i + 1]).startsWith("-") ? null : args[i + 1];
    } else if (typeof a === "string" && a.startsWith(eq)) {
      val = a.slice(eq.length);
    }
  }
  return val;
}

/**
 * Resolve explicit --web / --no-web. Last occurrence wins (split or equals).
 * Returns true when --web last, false when --no-web last, null when neither.
 * Prefix-safe: `--web-search` does not count as `--web`.
 * @param {string[]|null|undefined} args
 * @returns {boolean|null}
 */
export function resolveWebFlag(args) {
  if (!Array.isArray(args)) return null;
  let last = null;
  for (const a of args) {
    if (typeof a !== "string") continue;
    if (a === "--web" || a.startsWith("--web=")) last = true;
    else if (a === "--no-web" || a.startsWith("--no-web=")) last = false;
  }
  return last;
}

export function stripFlags(args) {
  const out = [];
  let pretty = false;
  let runMode = null;
  let integration = null;
  let jsonOut = false;
  let base = null;
  let resume = false;
  let fresh = false;
  let noNotify = false;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--pretty") {
      pretty = true;
      continue;
    }
    if (a === "--json") {
      jsonOut = true;
      continue;
    }
    if (a === "--resume") {
      resume = true;
      continue;
    }
    if (a === "--fresh") {
      fresh = true;
      continue;
    }
    // Companion-only: suppress terminal completion notify for this invocation.
    if (a === "--no-notify") {
      noNotify = true;
      continue;
    }
    if (a === "--run-mode" && args[i + 1]) {
      runMode = args[++i];
      continue;
    }
    if (typeof a === "string" && a.startsWith("--run-mode=")) {
      runMode = a.slice("--run-mode=".length);
      continue;
    }
    // Integration (how edits land) - resolved + consent-gated for code/implement.
    if (a === "--integration" && args[i + 1]) {
      integration = args[++i];
      continue;
    }
    if (typeof a === "string" && a.startsWith("--integration=")) {
      integration = a.slice("--integration=".length);
      continue;
    }
    if (a === "--base" && args[i + 1]) {
      // Captured for review framing; re-attached for code mode later.
      base = args[++i];
      continue;
    }
    if (typeof a === "string" && a.startsWith("--base=")) {
      // Equals form (the hardened wrapper's argparse accepts it): capture it too,
      // or direct review/code silently drops the base comparison.
      base = a.slice("--base=".length);
      continue;
    }
    out.push(a);
  }
  return { args: out, pretty, runMode, integration, jsonOut, base, resume, fresh, noNotify };
}
