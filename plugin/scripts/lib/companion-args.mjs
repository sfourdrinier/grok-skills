// plugin/scripts/lib/companion-args.mjs
//
// Pure argv flag-stripping for the companion entrypoint (extracted to keep
// grok-companion.mjs under the 900-line cap).

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
    out.push(a);
  }
  return { args: out, pretty, runMode, integration, jsonOut, base, resume, fresh, noNotify };
}

