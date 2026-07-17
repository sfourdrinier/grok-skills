// plugin/scripts/lib/render.mjs
// Human-readable views of Grok envelopes (stdout stays machine JSON by default).

/**
 * Parse a Grok result envelope from companion/wrapper stdout.
 * Prefers whole-string JSON; otherwise takes the last line that is a JSON object
 * with envelope-shaped fields (status / runId / mode).
 *
 * @param {string|null|undefined} text
 * @returns {object|null}
 */
export function tryParseEnvelope(text) {
  if (!text || typeof text !== "string") {
    return null;
  }
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }
  if (trimmed.startsWith("{")) {
    try {
      const whole = JSON.parse(trimmed);
      if (whole && typeof whole === "object" && !Array.isArray(whole)) {
        return whole;
      }
    } catch {
      /* fall through to line scan */
    }
  }
  const lines = trimmed.split(/\r?\n/);
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const line = lines[i].trim();
    if (!line.startsWith("{") || !line.endsWith("}")) {
      continue;
    }
    try {
      const obj = JSON.parse(line);
      if (
        obj &&
        typeof obj === "object" &&
        !Array.isArray(obj) &&
        (obj.status != null || obj.runId != null || obj.mode != null)
      ) {
        return obj;
      }
    } catch {
      /* continue */
    }
  }
  return null;
}

export function renderEnvelopePretty(envelope) {
  if (!envelope || typeof envelope !== "object") {
    return String(envelope ?? "");
  }
  const lines = [];
  lines.push(`# Grok ${envelope.mode ?? "run"} - ${envelope.status ?? "unknown"}`);
  if (envelope.runId) {
    lines.push(`Run id: \`${envelope.runId}\``);
  }
  if (envelope.error) {
    lines.push("");
    lines.push(`## Error (${envelope.error.class ?? "unknown"})`);
    lines.push(envelope.error.message ?? "");
    if (envelope.error.detail) {
      lines.push("");
      lines.push("```json");
      lines.push(JSON.stringify(envelope.error.detail, null, 2));
      lines.push("```");
    }
  }
  const response = envelope.response;
  if (typeof response === "string" && response.trim()) {
    lines.push("");
    lines.push("## Response");
    lines.push(response.trim());
  } else if (response && typeof response === "object") {
    if (typeof response.text === "string" && response.text.trim()) {
      lines.push("");
      lines.push("## Response");
      lines.push(response.text.trim());
    }
    if (Array.isArray(response.findings) && response.findings.length) {
      lines.push("");
      lines.push("## Findings");
      for (const f of response.findings) {
        const sev = f.severity ?? f.level ?? "info";
        const title = f.title ?? f.summary ?? f.message ?? JSON.stringify(f);
        lines.push(`- **[${sev}]** ${title}`);
        if (f.file || f.path) {
          lines.push(`  - file: \`${f.file ?? f.path}${f.line != null ? `:${f.line}` : ""}\``);
        }
        if (f.detail || f.description) {
          lines.push(`  - ${f.detail ?? f.description}`);
        }
      }
    }
    if (response.verdict || envelope.verifier?.verdict) {
      lines.push("");
      lines.push(`## Verdict: **${response.verdict ?? envelope.verifier.verdict}**`);
    }
  }
  if (Array.isArray(envelope.warnings) && envelope.warnings.length) {
    lines.push("");
    lines.push("## Warnings");
    for (const w of envelope.warnings) {
      lines.push(`- ${w}`);
    }
  }
  if (envelope.worktreePath) {
    lines.push("");
    lines.push(`Worktree: \`${envelope.worktreePath}\``);
  }
  if (Array.isArray(envelope.changedFiles) && envelope.changedFiles.length) {
    lines.push("");
    lines.push("## Changed files");
    for (const f of envelope.changedFiles) {
      lines.push(`- \`${f}\``);
    }
  }
  if (Array.isArray(envelope.citations) && envelope.citations.length) {
    lines.push("");
    lines.push("## Sources");
    for (const c of envelope.citations) {
      if (typeof c === "string") {
        lines.push(`- ${c}`);
        continue;
      }
      const url = c.url ?? c.href ?? "";
      const title = c.title ? ` | ${c.title}` : "";
      const grounded = c.grounded ? ` | ${c.grounded}` : "";
      lines.push(`- ${url}${title}${grounded}`);
    }
  }
  lines.push("");
  return lines.join("\n");
}

export function renderSetupReport(info) {
  const lines = ["# Grok Skills setup", ""];
  lines.push(`| Check | Result |`);
  lines.push(`| --- | --- |`);
  for (const row of info.rows ?? []) {
    lines.push(`| ${row.name} | ${row.ok ? "ok" : "FAIL"} - ${row.detail} |`);
  }
  lines.push("");
  lines.push(`Run mode for this workspace: **${info.runMode}** (hardened = default sandbox path; direct = installed Grok CLI home).`);
  lines.push("");
  if (info.hints?.length) {
    lines.push("## Next steps");
    for (const h of info.hints) {
      lines.push(`- ${h}`);
    }
    lines.push("");
  }
  return lines.join("\n");
}
