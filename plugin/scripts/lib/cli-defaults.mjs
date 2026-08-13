// plugin/scripts/lib/cli-defaults.mjs
//
// Product defaults for the installed Grok CLI child. Data:
// plugin/references/grok-cli-defaults.json (parity with Python groklib.cli_defaults).
// No second default-model or effort vocabulary.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { isFlagToken } from "./companion-args.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const CLI_DEFAULTS_SSOT_PATH = path.resolve(
  HERE,
  "../../references/grok-cli-defaults.json"
);

let _doc = null;

export function parseCliDefaultsDoc(doc) {
  if (!doc || typeof doc !== "object") {
    throw new Error("cli-defaults SSOT must be an object");
  }
  if (typeof doc.defaultModel !== "string" || !doc.defaultModel.trim()) {
    throw new Error("cli-defaults SSOT missing defaultModel");
  }
  if (
    !Array.isArray(doc.reasoningEffortValues) ||
    doc.reasoningEffortValues.length === 0 ||
    !doc.reasoningEffortValues.every((v) => typeof v === "string" && v)
  ) {
    throw new Error("cli-defaults SSOT has empty/invalid reasoningEffortValues");
  }
  if (typeof doc.noPlanDefault !== "boolean") {
    throw new Error("cli-defaults SSOT noPlanDefault must be a boolean");
  }
  return doc;
}

export function loadCliDefaults() {
  if (_doc) return _doc;
  if (!fs.existsSync(CLI_DEFAULTS_SSOT_PATH)) {
    throw new Error(`cli-defaults SSOT missing at ${CLI_DEFAULTS_SSOT_PATH}`);
  }
  _doc = parseCliDefaultsDoc(JSON.parse(fs.readFileSync(CLI_DEFAULTS_SSOT_PATH, "utf8")));
  return _doc;
}

export function defaultModel() {
  return loadCliDefaults().defaultModel;
}

export function reasoningEffortValues() {
  return loadCliDefaults().reasoningEffortValues.slice();
}

export function noPlanDefault() {
  return loadCliDefaults().noPlanDefault;
}

export function isSameModelFamily(effective, requested) {
  if (typeof effective !== "string" || typeof requested !== "string") return false;
  return effective === requested || effective.startsWith(`${requested}-`);
}

export function effectiveModelFromUsage(modelUsage) {
  if (!modelUsage || typeof modelUsage !== "object" || Array.isArray(modelUsage)) {
    return null;
  }
  for (const key of Object.keys(modelUsage)) {
    if (typeof key === "string" && key) return key;
  }
  return null;
}

export function parseReasoningEffort(raw) {
  if (typeof raw !== "string") {
    const err = new Error("reasoning effort must be a string");
    err.code = "usage-error";
    throw err;
  }
  const value = raw.trim().toLowerCase();
  const allowed = loadCliDefaults().reasoningEffortValues;
  if (!allowed.includes(value)) {
    const err = new Error(
      `reasoning effort must be one of ${allowed.join(", ")}; got ${JSON.stringify(raw)}`
    );
    err.code = "usage-error";
    throw err;
  }
  return value;
}

export function parseRequestedModel(raw) {
  if (typeof raw !== "string" || !raw.trim()) {
    const err = new Error("model id must be a non-empty string");
    err.code = "usage-error";
    throw err;
  }
  return raw.trim();
}

function takeEffortToken(token) {
  if (typeof token !== "string") return null;
  if (token.startsWith("--reasoning-effort=")) return token.slice("--reasoning-effort=".length);
  if (token.startsWith("--effort=")) return token.slice("--effort=".length);
  return null;
}

export function resolveReasoningEffort(args) {
  if (!Array.isArray(args)) return null;
  let last = null;
  let seen = false;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--reasoning-effort" || a === "--effort") {
      const next = args[i + 1];
      seen = true;
      last = parseReasoningEffort(
        next !== undefined && !isFlagToken(next) ? String(next) : ""
      );
      continue;
    }
    const eq = takeEffortToken(a);
    if (eq !== null) {
      seen = true;
      last = parseReasoningEffort(eq);
    }
  }
  if (!seen) return null;
  return last;
}

export function resolveNoPlan(args) {
  if (!Array.isArray(args)) return noPlanDefault();
  let last = noPlanDefault();
  for (const a of args) {
    if (typeof a !== "string") continue;
    if (a === "--plan") {
      last = false;
      continue;
    }
    if (a === "--no-plan") {
      last = true;
      continue;
    }
    if (a.startsWith("--plan=") || a.startsWith("--no-plan=")) {
      const err = new Error(
        "valued --plan=/--no-plan= is invalid; use bare --plan or --no-plan"
      );
      err.code = "usage-error";
      throw err;
    }
  }
  return last;
}

export function resolveRequestedModel(args) {
  if (!Array.isArray(args)) return defaultModel();
  let seen = false;
  let last = null;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--model") {
      const next = args[i + 1];
      seen = true;
      last = parseRequestedModel(
        next !== undefined && !isFlagToken(next) ? String(next) : ""
      );
      continue;
    }
    if (typeof a === "string" && a.startsWith("--model=")) {
      seen = true;
      last = parseRequestedModel(a.slice("--model=".length));
    }
  }
  if (!seen) return defaultModel();
  return last;
}
