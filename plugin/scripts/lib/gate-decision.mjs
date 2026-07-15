// plugin/scripts/lib/gate-decision.mjs
//
// Pure decision logic for the stop-time review gate. Fail closed: free-text
// review "success" alone never opens the session. Allow only when:
//   - verify-style verifier.verdict === "pass", or
//   - structured findings exist and none are critical/high.
// Anything else blocks with an actionable reason.

const SETUP_HINT =
  "Run /grok:setup (and authenticate the grok CLI if needed), then retry or disable the gate with /grok:setup --disable-review-gate.";

const BLOCKING_SEVERITIES = new Set(["critical", "high"]);

/**
 * @param {unknown} envelope
 * @returns {{ ok: boolean, reason: string | null }}
 */
export function decideFromEnvelope(envelope) {
  if (typeof envelope !== "object" || envelope === null) {
    return {
      ok: false,
      reason: `Grok stop-gate could not read a result envelope from the review run. ${SETUP_HINT}`,
    };
  }

  const status = envelope.status;
  if (typeof status !== "string") {
    return {
      ok: false,
      reason: `Grok stop-gate got an envelope with no status field. ${SETUP_HINT}`,
    };
  }

  if (status === "success") {
    const verifier = envelope.verifier;
    if (verifier && typeof verifier === "object" && typeof verifier.verdict === "string") {
      if (verifier.verdict === "pass") {
        return { ok: true, reason: null };
      }
      return {
        ok: false,
        reason: `Grok verify verdict is "${verifier.verdict}" (not "pass"); fix the flagged issues before ending the session.`,
      };
    }

    const response = envelope.response;
    const structured =
      response && typeof response === "object" && response.structured && typeof response.structured === "object"
        ? response.structured
        : null;
    const findings = structured && Array.isArray(structured.findings) ? structured.findings : null;

    if (findings === null) {
      return {
        ok: false,
        reason:
          `Grok stop-gate requires machine-readable findings (schema-backed review) or a verify pass verdict; free-text success is not enough to end the session. ${SETUP_HINT}`,
      };
    }

    const blocking = findings.filter((item) => {
      if (!item || typeof item !== "object") {
        return false;
      }
      const severity = String(item.severity ?? "").toLowerCase();
      return BLOCKING_SEVERITIES.has(severity);
    });
    if (blocking.length > 0) {
      const titles = blocking
        .slice(0, 3)
        .map((item) => String(item.title ?? item.severity ?? "finding"))
        .join("; ");
      return {
        ok: false,
        reason: `Grok stop-gate found ${blocking.length} critical/high finding(s) (e.g. ${titles}); fix them before ending the session.`,
      };
    }
    return { ok: true, reason: null };
  }

  const error = envelope.error;
  const errorClass =
    error && typeof error === "object" && typeof error.class === "string" ? error.class : "unknown";
  const errorMessage =
    error && typeof error === "object" && typeof error.message === "string" ? error.message : "no message";

  if (errorClass === "auth-missing" || errorClass === "probe-required" || errorClass === "version-mismatch") {
    return {
      ok: false,
      reason: `Grok stop-gate could not run the review (${errorClass}: ${errorMessage}). ${SETUP_HINT}`,
    };
  }

  return {
    ok: false,
    reason: `Grok stop-gate review run failed (${errorClass}: ${errorMessage}); resolve it before ending the session.`,
  };
}

/**
 * @param {{ error?: { code?: string, message?: string } | null, status?: number | null, stdout?: string }} result
 * @returns {{ ok: boolean, reason: string | null }}
 */
export function classifyReviewRun(result) {
  if (result && result.error) {
    if (result.error.code === "ETIMEDOUT") {
      return {
        ok: false,
        reason: `Grok stop-gate review timed out. Run /grok:review --wait manually, or disable the gate with /grok:setup --disable-review-gate.`,
      };
    }
    const code = typeof result.error.code === "string" ? result.error.code : "unknown";
    const message = typeof result.error.message === "string" ? result.error.message : "no message";
    return {
      ok: false,
      reason: `Grok stop-gate could not spawn the review run (${code}: ${message}); resolve the local condition, or disable the gate with /grok:setup --disable-review-gate.`,
    };
  }

  const stdout = typeof result?.stdout === "string" ? result.stdout.trim() : "";
  if (!stdout) {
    return {
      ok: false,
      reason: `Grok stop-gate review produced no result envelope on stdout. ${SETUP_HINT}`,
    };
  }

  let envelope;
  try {
    envelope = JSON.parse(stdout);
  } catch (err) {
    return {
      ok: false,
      reason: `Grok stop-gate review returned output that is not a valid JSON envelope (${err.message}). ${SETUP_HINT}`,
    };
  }

  return decideFromEnvelope(envelope);
}
