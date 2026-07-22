// plugin/scripts/lib/companion-setup.mjs
//
// /grok:setup implementation (gate, run mode, notifications, codex agents,
// preflight report). Extracted from grok-companion.mjs for the 900-line cap.

import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";

import { flagValue, hasFlagOrEquals } from "./companion-args.mjs";
import {
  getCodexAgentsScope,
  installCodexAgents,
  parseCodexAgentsScope,
  setCodexAgentsScope,
  uninstallCodexAgents,
} from "./codex-agents.mjs";
import { grokBinaryAvailable } from "./direct-grok.mjs";
import { readGateConfig, writeGateConfig } from "./gate-state.mjs";
import {
  parseTargetFlag,
  resolveTargetWorkspaceRoot,
} from "./git-context.mjs";
import {
  getIntegrationMode,
  getNotificationConfig,
  getRunMode,
  INTEGRATION_MODES,
  NOTIFICATION_MODES,
  parseIntegrationMode,
  parseNotificationMode,
  parseWebhookUrl,
  setIntegrationMode,
  setNotificationConfig,
  setRunMode,
} from "./jobs.mjs";
import { formatWebhookDisplay } from "./notification-modes.mjs";
import { wrapperChildEnv } from "./notify.mjs";
import { renderSetupReport, tryParseEnvelope } from "./render.mjs";
import { resolveWrapperPath } from "./wrapper.mjs";

/**
 * @param {string} cwd
 * @param {string[]} args
 * @param {{ python?: string, pluginRoot: string }} opts
 * @returns {number} process exit code
 */
export function cmdSetup(cwd, args, { python = "python3", pluginRoot }) {
  const enable = args.includes("--enable-review-gate");
  const disable = args.includes("--disable-review-gate");
  const skipCodexAgents = args.includes("--skip-codex-agents");
  const forceCodexAgents = args.includes("--force-codex-agents");
  const removeCodexAgents = args.includes("--remove-codex-agents");
  // Capture scope before other prefs writes (jobs-index may drop unknown keys).
  const priorCodexAgentsScope = getCodexAgentsScope(cwd, process.env);
  let scopeFlag = null;
  let invalidCodexAgentsScope = null;
  {
    // Presence SSOT (split OR equals) - never re-open local startsWith loops.
    if (hasFlagOrEquals(args, "--codex-agents-scope")) {
      const rawScope = flagValue(args, "--codex-agents-scope");
      if (rawScope === null) {
        invalidCodexAgentsScope = "(missing value)";
      } else {
        scopeFlag = parseCodexAgentsScope(rawScope);
        if (!scopeFlag) {
          invalidCodexAgentsScope = String(rawScope).trim() || rawScope;
        }
      }
    }
  }
  // runMode is security posture (hardened|direct). Do not treat --integration
  // direct/worktree/... values as run-mode (orthogonal axis). Split AND equals
  // forms are accepted (argv SSOT / wrapper argparse parity).
  {
    let mode = null;
    if (hasFlagOrEquals(args, "--run-mode")) {
      mode = flagValue(args, "--run-mode");
    } else {
      // Bare convenience: setup direct | setup hardened (not a flag value).
      for (let i = 0; i < args.length; i++) {
        const a = args[i];
        if (a !== "direct" && a !== "hardened") continue;
        const prev = args[i - 1];
        // Skip any value-bearing flag's argument: a --target/notification value
        // literally named "direct"/"hardened" must not be read as the bare
        // `setup direct` convenience (review: --target value misread as run mode).
        if (
          [
            "--integration",
            "--run-mode",
            "--target",
            "--notification-mode",
            "--notification-webhook-url",
            "--codex-agents-scope",
          ].includes(prev)
        )
          continue;
        mode = a;
        break;
      }
    }
    if (mode === "direct" || mode === "hardened") {
      setRunMode(cwd, mode);
    }
  }
  // Integration mode (how edits land) - orthogonal to runMode security posture.
  // integrationMode is target-scoped (resolved --target git toplevel, default
  // '.'). Other setup prefs (runMode, notifications, gate) stay companion-cwd
  // scoped. No consent gate (2.0.1+).
  const wantJson = args.includes("--json");
  const integrationTargetWorkspace = resolveTargetWorkspaceRoot(
    cwd,
    parseTargetFlag(args)
  );
  let invalidIntegrationMode = null;
  {
    if (hasFlagOrEquals(args, "--integration")) {
      const rawIntegration = flagValue(args, "--integration");
      if (rawIntegration === null) {
        invalidIntegrationMode = "(missing value)";
      } else {
        const parsedIntegration = parseIntegrationMode(rawIntegration);
        if (!parsedIntegration) {
          invalidIntegrationMode = String(rawIntegration).trim() || rawIntegration;
        } else {
          setIntegrationMode(integrationTargetWorkspace, parsedIntegration);
        }
      }
    }
  }
  // Notification prefs: parse all flags first; apply atomically or apply none.
  // Equals forms (`--notification-mode=auto`) are accepted via the argv SSOT.
  let invalidNotificationMode = null;
  let invalidWebhookUrl = null;
  /** @type {{ notificationMode?: string, notificationWebhookUrl?: string|null }} */
  const notifyPatch = {};
  {
    if (hasFlagOrEquals(args, "--notification-mode")) {
      const rawMode = flagValue(args, "--notification-mode");
      if (rawMode === null) {
        invalidNotificationMode = "(missing value)";
      } else {
        const parsedMode = parseNotificationMode(rawMode);
        if (!parsedMode) {
          invalidNotificationMode = String(rawMode).trim().toLowerCase() || rawMode;
        } else {
          notifyPatch.notificationMode = parsedMode;
        }
      }
    }
  }
  {
    if (hasFlagOrEquals(args, "--notification-webhook-url")) {
      const rawUrl = flagValue(args, "--notification-webhook-url");
      if (rawUrl === null) {
        invalidWebhookUrl = "webhook-url-missing-value";
      } else {
        const parsedWebhook = parseWebhookUrl(rawUrl);
        if (!parsedWebhook.ok) {
          invalidWebhookUrl = parsedWebhook.reason || "webhook-url-invalid";
        } else {
          notifyPatch.notificationWebhookUrl = parsedWebhook.url;
        }
      }
    }
  }
  const notifyPrefsInvalid = Boolean(invalidNotificationMode || invalidWebhookUrl);
  if (!notifyPrefsInvalid && Object.keys(notifyPatch).length > 0) {
    setNotificationConfig(cwd, notifyPatch);
  }
  if (enable) writeGateConfig(cwd, true);
  if (disable) writeGateConfig(cwd, false);

  // Re-persist scope after other prefs writes so jobs-index rewrites cannot drop it.
  const effectiveCodexAgentsScope = scopeFlag || priorCodexAgentsScope;
  if (!invalidCodexAgentsScope) {
    setCodexAgentsScope(cwd, effectiveCodexAgentsScope, process.env);
  }

  const gate = readGateConfig(cwd);
  const runMode = getRunMode(cwd);
  const integrationMode = getIntegrationMode(integrationTargetWorkspace);
  const notifyPrefs = getNotificationConfig(cwd);
  const codexAgentsScope = invalidCodexAgentsScope
    ? priorCodexAgentsScope
    : getCodexAgentsScope(cwd, process.env);
  const binary = grokBinaryAvailable();
  const wrapper = resolveWrapperPath(process.env);
  const webhookDetail = formatWebhookDisplay(notifyPrefs.notificationWebhookUrl);
  let notificationsDetail;
  if (invalidNotificationMode) {
    notificationsDetail = `invalid mode ${JSON.stringify(invalidNotificationMode)} (notification prefs unchanged)`;
  } else if (invalidWebhookUrl) {
    notificationsDetail = `invalid webhook URL (${invalidWebhookUrl}; notification prefs unchanged)`;
  } else {
    notificationsDetail = `${notifyPrefs.notificationMode}${
      notifyPrefs.notificationWebhookUrl ? `; webhook=${webhookDetail}` : ""
    }`;
  }
  let integrationDetail;
  if (invalidIntegrationMode) {
    integrationDetail = `invalid ${JSON.stringify(invalidIntegrationMode)} (integration prefs unchanged)`;
  } else {
    integrationDetail = integrationMode;
  }
  const rows = [
    {
      name: "grok CLI",
      ok: binary.ok,
      detail: binary.ok ? binary.version : binary.detail || "missing",
    },
    {
      name: "wrapper",
      ok: Boolean(wrapper),
      detail: wrapper || "not found",
    },
    {
      name: "run mode",
      ok: true,
      detail: runMode,
    },
    {
      name: "integration mode",
      ok: !invalidIntegrationMode,
      detail: integrationDetail,
    },
    {
      name: "notifications",
      ok: !notifyPrefsInvalid,
      detail: notificationsDetail,
    },
    {
      name: "stop-review gate",
      ok: true,
      detail: gate.stopReviewGate ? "ENABLED" : "disabled",
    },
    {
      name: "codex agents scope",
      ok: !invalidCodexAgentsScope,
      detail: invalidCodexAgentsScope
        ? `invalid ${JSON.stringify(invalidCodexAgentsScope)} (scope prefs unchanged; valid: user|project)`
        : codexAgentsScope,
    },
  ];
  const hints = [];
  if (!binary.ok) {
    hints.push("Install and authenticate the Grok CLI, then re-run /grok:setup.");
    hints.push("See https://x.ai for Grok CLI install docs for your platform.");
  }
  if (!wrapper) {
    hints.push("Reinstall the plugin so plugin/wrapper/scripts/grok_agent.py is present.");
  }
  if (runMode === "direct") {
    hints.push(
      "Direct mode uses your installed Grok home (like OpenAI's plugin uses installed Codex). Switch with: companion setup --run-mode hardened"
    );
  } else {
    hints.push(
      "Hardened mode is default. For installed-CLI posture: companion setup --run-mode direct"
    );
  }
  if (invalidIntegrationMode) {
    hints.push(
      `Valid --integration values: ${INTEGRATION_MODES.join(" | ")}. Integration prefs were not changed.`
    );
  } else if (integrationMode === "direct") {
    hints.push(
      "Direct integration is the product default (live-tree one-shot code). " +
        "For isolation: setup --integration worktree|auto|review."
    );
  }
  if (invalidNotificationMode) {
    hints.push(
      `Valid --notification-mode values: ${NOTIFICATION_MODES.join(" | ")}. No notification prefs were written.`
    );
  } else if (invalidWebhookUrl) {
    hints.push(
      "Webhook URL must be an absolute http(s) URL. No notification prefs were written."
    );
  } else if (notifyPrefs.notificationMode === "off") {
    hints.push(
      "Notifications are off. For background completion signals: setup --notification-mode auto."
    );
  }
  if (invalidCodexAgentsScope) {
    hints.push(
      "Valid --codex-agents-scope values: user | project. Scope prefs were not changed."
    );
  } else if (codexAgentsScope === "project") {
    hints.push(
      "Codex agents scope=project: managed TOMLs install into <cwd>/.codex/agents (SessionStart honors this prefs)."
    );
  }

  let agentsResult = null;
  let agentsOk = true;
  if (removeCodexAgents) {
    agentsResult = uninstallCodexAgents({
      env: process.env,
      backup: true,
      cwd,
      scope: codexAgentsScope,
    });
    const detail = agentsResult.ok
      ? `scope=${codexAgentsScope} removed=[${agentsResult.removed.join(", ") || "none"}] user-owned-kept=[${agentsResult.skippedUser.join(", ") || "none"}] backups=[${agentsResult.backedUp.join(", ") || "none"}] → ${agentsResult.destDir}`
      : `errors: ${agentsResult.errors.join("; ")}`;
    rows.push({ name: "codex agents", ok: agentsResult.ok, detail });
    agentsOk = agentsResult.ok;
    if (agentsResult.removed.length) {
      hints.push(
        "Removed managed Codex agents (backups as *.toml.bak). SessionStart will reinstall while the plugin is enabled."
      );
    }
  } else if (!skipCodexAgents) {
    agentsResult = installCodexAgents({
      env: process.env,
      force: forceCodexAgents,
      updateManaged: true,
      pluginRoot,
      backup: true,
      cwd,
      scope: codexAgentsScope,
    });
    const parts = [
      `scope=${codexAgentsScope}`,
      `installed=[${agentsResult.installed.join(", ") || "none"}]`,
      `updated=[${agentsResult.updated.join(", ") || "none"}]`,
      `skipped=[${agentsResult.skipped.join(", ") || "none"}]`,
      agentsResult.skippedUser?.length
        ? `user-owned=[${agentsResult.skippedUser.join(", ")}]`
        : null,
      agentsResult.backedUp?.length ? `backups=[${agentsResult.backedUp.join(", ")}]` : null,
      `→ ${agentsResult.destDir}`,
    ].filter(Boolean);
    const detail = agentsResult.ok
      ? parts.join(" ")
      : `errors: ${agentsResult.errors.join("; ")}`;
    rows.push({
      name: "codex agents",
      ok: agentsResult.ok,
      detail,
    });
    agentsOk = agentsResult.ok;
    if (agentsResult.installed.length || agentsResult.updated.length) {
      hints.push(
        "Codex agents ready (absolute GROK_AGENT_RUN → agents/run.mjs): grok-engineer-coder, grok-rescue. Also auto-installed on SessionStart."
      );
    } else if (agentsResult.skippedUser?.length) {
      hints.push(
        "Some grok-*.toml agents look user-owned (no managed-by header). Use setup --force-codex-agents to overwrite (creates .bak)."
      );
    } else if (agentsResult.skipped.length && !agentsResult.installed.length) {
      hints.push(
        `Codex agents already up to date under ${agentsResult.destDir} (SessionStart keeps managed agents in sync).`
      );
    }
  }

  let preflightOk = true;
  if (wrapper && runMode === "hardened") {
    const pre = spawnSync(python, [wrapper, "preflight"], {
      encoding: "utf8",
      env: wrapperChildEnv(process.env),
    });
    if (pre.status !== 0 && pre.status != null) {
      preflightOk = false;
    }
    if (pre.stdout) {
      const env = tryParseEnvelope(pre.stdout);
      if (env?.response?.checks) {
        for (const c of env.response.checks) {
          const ok = Boolean(c.ok);
          if (!ok) {
            preflightOk = false;
          }
          rows.push({ name: `preflight:${c.name}`, ok, detail: c.detail || "" });
        }
      } else {
        process.stderr.write(pre.stderr || "");
      }
    } else if (pre.status !== 0 && pre.status != null) {
      rows.push({
        name: "preflight",
        ok: false,
        detail: (pre.stderr || `exit ${pre.status}`).trim() || "preflight failed",
      });
    }
  }

  const ok =
    binary.ok &&
    Boolean(wrapper) &&
    agentsOk &&
    !notifyPrefsInvalid &&
    !invalidCodexAgentsScope &&
    !invalidIntegrationMode &&
    preflightOk;
  // Issue #8: machine-readable setup for orchestrators (consent-free status).
  if (wantJson) {
    process.stdout.write(
      `${JSON.stringify({
        schemaVersion: 1,
        mode: "setup",
        status: ok ? "success" : "failure",
        runMode,
        integrationMode,
        targetWorkspace: integrationTargetWorkspace,
        notifications: {
          mode: notifyPrefs.notificationMode,
          // Never dump raw webhook URL (secrets often live in path); parity with human report.
          webhookUrl: notifyPrefs.notificationWebhookUrl
            ? formatWebhookDisplay(notifyPrefs.notificationWebhookUrl)
            : null,
          webhookConfigured: Boolean(notifyPrefs.notificationWebhookUrl),
        },
        stopReviewGate: Boolean(gate.stopReviewGate),
        codexAgentsScope,
        checks: rows.map((r) => ({
          name: r.name,
          ok: Boolean(r.ok),
          detail: r.detail || "",
        })),
        hints,
        grokCli: binary.ok ? binary.version : null,
        wrapper: wrapper || null,
      })}\n`
    );
  } else {
    process.stdout.write(renderSetupReport({ rows, runMode, hints }));
  }
  return ok
    ? 0
    : 1;
}
