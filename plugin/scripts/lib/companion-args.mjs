// plugin/scripts/lib/companion-args.mjs
//
// Single source of truth for companion argv flag parsing (split, equals, presence,
// strip, and value-bearing consume). All companion consumers must reuse these
// helpers so split/equals/duplicate semantics stay consistent with the wrapper's
// argparse and with each other.

/**
 * True when a token looks like a CLI flag rather than a flag value.
 * The stdin/path sentinel "-" is a value, not a flag.
 * @param {unknown} token
 * @returns {boolean}
 */
export function isFlagToken(token) {
  return typeof token === "string" && token.startsWith("-") && token !== "-";
}

/** True if `args` contains `name` in split (`--x`) OR equals (`--x=v`) form.
 *  Presence check parity with the wrapper's argparse (which accepts both). */
export function hasFlagOrEquals(args, name) {
  const eq = name + "=";
  return Array.isArray(args) && args.some((a) => a === name || (typeof a === "string" && a.startsWith(eq)));
}

/**
 * Walk argv and yield every occurrence of a named flag.
 * Split form records a value only when the next token is a valid non-flag value;
 * a following flag is never consumed as the value (caller may keep prior good).
 * @param {string[]|null|undefined} args
 * @param {string} name e.g. "--task"
 * @returns {{ index: number, form: "split"|"equals", value: string|null, valueIndex: number|null }[]}
 */
export function flagOccurrences(args, name) {
  if (!Array.isArray(args) || typeof name !== "string" || !name) return [];
  const eq = name + "=";
  /** @type {{ index: number, form: "split"|"equals", value: string|null, valueIndex: number|null }[]} */
  const out = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === name) {
      const next = args[i + 1];
      if (next !== undefined && !isFlagToken(next)) {
        out.push({ index: i, form: "split", value: String(next), valueIndex: i + 1 });
      } else {
        out.push({ index: i, form: "split", value: null, valueIndex: null });
      }
    } else if (typeof a === "string" && a.startsWith(eq)) {
      out.push({ index: i, form: "equals", value: a.slice(eq.length), valueIndex: i });
    }
  }
  return out;
}

/**
 * Last *valid* value for `--name value` or `--name=value` (argparse parity).
 * Does not consume a following flag as the value. A later invalid bare duplicate
 * (e.g. `--task --target`) does NOT wipe a prior good value.
 * Returns null when no valid occurrence is present.
 * @param {string[]|null|undefined} args
 * @param {string} name e.g. "--task"
 * @returns {string|null}
 */
export function flagValue(args, name) {
  const occ = flagOccurrences(args, name);
  let val = null;
  for (const o of occ) {
    if (o.value !== null) val = o.value;
  }
  return val;
}

/**
 * First *valid* value for `--name value` or `--name=value`.
 * Used where first-wins is intentional (e.g. direct-mode --run-id refusal).
 * @param {string[]|null|undefined} args
 * @param {string} name
 * @returns {string|null}
 */
export function firstFlagValue(args, name) {
  for (const o of flagOccurrences(args, name)) {
    if (o.value !== null) return o.value;
  }
  return null;
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

/**
 * Strip one value-bearing flag (split + equals) without consuming a following flag
 * as its value. Last valid value is returned; the flag tokens (and valid values)
 * are omitted from `args`.
 * @param {string[]} args
 * @param {string} name
 * @returns {{ args: string[], value: string|null }}
 */
export function stripValueFlag(args, name) {
  if (!Array.isArray(args) || typeof name !== "string" || !name) {
    return { args: Array.isArray(args) ? args.slice() : [], value: null };
  }
  const eq = name + "=";
  const out = [];
  let value = null;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === name) {
      const next = args[i + 1];
      if (next !== undefined && !isFlagToken(next)) {
        value = String(next);
        i += 1;
      }
      continue;
    }
    if (typeof a === "string" && a.startsWith(eq)) {
      value = a.slice(eq.length);
      continue;
    }
    out.push(a);
  }
  return { args: out, value };
}

/**
 * Drop any occurrence of the named value-bearing flags (split + equals) without
 * consuming a following flag as a value. Does not append replacements.
 * @param {string[]} args
 * @param {string[]} names
 * @returns {string[]}
 */
export function dropValueFlags(args, names) {
  if (!Array.isArray(args)) return [];
  if (!Array.isArray(names) || names.length === 0) return args.slice();
  const nameSet = new Set(names);
  const eqPrefixes = names.map((n) => n + "=");
  const out = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (nameSet.has(a)) {
      const next = args[i + 1];
      if (next !== undefined && !isFlagToken(next)) i += 1;
      continue;
    }
    if (typeof a === "string" && eqPrefixes.some((p) => a.startsWith(p))) {
      continue;
    }
    out.push(a);
  }
  return out;
}

/**
 * Companion-only flags peeled before dispatch. Value-bearing flags reuse
 * stripValueFlag (split + equals, never consume a following flag; last valid
 * wins). Boolean companion flags are peeled in one pass.
 * @param {string[]} args
 * @returns {{
 *   args: string[],
 *   pretty: boolean,
 *   runMode: string|null,
 *   integration: string|null,
 *   jsonOut: boolean,
 *   base: string|null,
 *   resume: boolean,
 *   fresh: boolean,
 *   noNotify: boolean,
 * }}
 */
export function stripFlags(args) {
  let pretty = false;
  let jsonOut = false;
  let resume = false;
  let fresh = false;
  let noNotify = false;
  const boolOut = [];
  const list = Array.isArray(args) ? args : [];
  for (const a of list) {
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
    boolOut.push(a);
  }
  // Value-bearing peel via SSOT (flagOccurrences / stripValueFlag): missing values
  // stay null; equals forms capture; a following flag is never consumed as a value.
  const runModeStripped = stripValueFlag(boolOut, "--run-mode");
  const integrationStripped = stripValueFlag(runModeStripped.args, "--integration");
  const baseStripped = stripValueFlag(integrationStripped.args, "--base");
  return {
    args: baseStripped.args,
    pretty,
    runMode: runModeStripped.value,
    integration: integrationStripped.value,
    jsonOut,
    base: baseStripped.value,
    resume,
    fresh,
    noNotify,
  };
}
