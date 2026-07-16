// plugin/scripts/lib/companion-setup.mjs
//
// /grok:setup implementation (gate, run mode, notifications, codex agents,
// preflight report). Extracted from grok-companion.mjs for the 900-line cap.

import { spawnSync } from "node:child_process";
import process from "node:process";

import { installCodexAgents, uninstallCodexAgents } from "./codex-agents.mjs";
import { grokBinaryAvailable } from "./direct-grok.mjs";
import { readGateConfig, writeGateConfig } from "./gate-state.mjs";
import {
  getNotificationConfig,
  getRunMode,
  NOTIFICATION_MODES,
  parseNotificationMode,
  parseWebhookUrl,
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
  if (args.includes("--run-mode") || args.includes("direct") || args.includes("hardened")) {
    const idx = args.indexOf("--run-mode");
    const mode = idx >= 0 ? args[idx + 1] : args.find((a) => a === "direct" || a === "hardened");
    if (mode === "direct" || mode === "hardened") {
      setRunMode(cwd, mode);
    }
  }
  // Notification prefs: parse all flags first; apply atomically or apply none.
  let invalidNotificationMode = null;
  let invalidWebhookUrl = null;
  /** @type {{ notificationMode?: string, notificationWebhookUrl?: string|null }} */
  const notifyPatch = {};
  const notifyModeIdx = args.indexOf("--notification-mode");
  if (notifyModeIdx >= 0) {
    const rawMode = args[notifyModeIdx + 1];
    if (rawMode === undefined || String(rawMode).startsWith("--")) {
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
  const webhookIdx = args.indexOf("--notification-webhook-url");
  if (webhookIdx >= 0) {
    const rawUrl = args[webhookIdx + 1];
    if (rawUrl === undefined || String(rawUrl).startsWith("--")) {
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
  const notifyPrefsInvalid = Boolean(invalidNotificationMode || invalidWebhookUrl);
  if (!notifyPrefsInvalid && Object.keys(notifyPatch).length > 0) {
    setNotificationConfig(cwd, notifyPatch);
  }
  if (enable) writeGateConfig(cwd, true);
  if (disable) writeGateConfig(cwd, false);

  const gate = readGateConfig(cwd);
  const runMode = getRunMode(cwd);
  const notifyPrefs = getNotificationConfig(cwd);
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
      name: "notifications",
      ok: !notifyPrefsInvalid,
      detail: notificationsDetail,
    },
    {
      name: "stop-review gate",
      ok: true,
      detail: gate.stopReviewGate ? "ENABLED" : "disabled",
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
      "Notifications are off. For background completion signals: setup --notification-mode auto (recommended)."
    );
  }

  let agentsResult = null;
  let agentsOk = true;
  if (removeCodexAgents) {
    agentsResult = uninstallCodexAgents({ env: process.env, backup: true });
    const detail = agentsResult.ok
      ? `removed=[${agentsResult.removed.join(", ") || "none"}] user-owned-kept=[${agentsResult.skippedUser.join(", ") || "none"}] backups=[${agentsResult.backedUp.join(", ") || "none"}] → ${agentsResult.destDir}`
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
    });
    const parts = [
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
        "Some ~/.codex/agents/grok-*.toml look user-owned (no managed-by header). Use setup --force-codex-agents to overwrite (creates .bak)."
      );
    } else if (agentsResult.skipped.length && !agentsResult.installed.length) {
      hints.push(
        "Codex agents already up to date under ~/.codex/agents (SessionStart keeps managed agents in sync)."
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

  process.stdout.write(renderSetupReport({ rows, runMode, hints }));
  return binary.ok && wrapper && agentsOk && !notifyPrefsInvalid && preflightOk ? 0 : 1;
}
