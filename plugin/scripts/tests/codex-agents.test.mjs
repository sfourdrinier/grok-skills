// plugin/scripts/tests/codex-agents.test.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  backupAgentFile,
  ensureCodexAgents,
  getCodexAgentsScope,
  installCodexAgents,
  isManagedAgentBody,
  listTemplateAgents,
  materializeAgentBody,
  resolveAgentRunPath,
  resolveCodexAgentsDestDir,
  resolveCompanionPath,
  setCodexAgentsScope,
  shellSingleQuote,
  uninstallCodexAgents,
} from "../lib/codex-agents.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TEMPLATES = path.resolve(HERE, "..", "..", "codex-agents");
const PLUGIN_ROOT = path.resolve(HERE, "..", "..");

test("listTemplateAgents finds shipped TOML agents", () => {
  const list = listTemplateAgents(TEMPLATES);
  const names = list.map((t) => t.name);
  assert.ok(names.includes("grok-engineer-coder"));
  assert.ok(names.includes("grok-rescue"));
});

test("shipped templates use agent-run placeholder and sandbox_mode", () => {
  for (const t of listTemplateAgents(TEMPLATES)) {
    const body = fs.readFileSync(t.source, "utf8");
    assert.ok(
      body.includes("__GROK_AGENT_RUN_Q__"),
      `${t.name} missing __GROK_AGENT_RUN_Q__`
    );
    assert.ok(!body.includes("${PLUGIN_ROOT"), `${t.name} still uses PLUGIN_ROOT`);
    assert.ok(
      !body.includes("${CLAUDE_PLUGIN_ROOT"),
      `${t.name} still uses CLAUDE_PLUGIN_ROOT`
    );
    assert.ok(
      /do not invent cache paths|NEVER invent cache paths|do not invent cache paths/i.test(
        body
      ),
      `${t.name} missing never-invent-paths guidance`
    );
    assert.match(body, /sandbox_mode\s*=\s*"read-only"/);
    assert.match(body, /GROK_AGENT_RUN/);
  }
});

test("shellSingleQuote escapes embedded single quotes", () => {
  assert.equal(shellSingleQuote("/tmp/x"), "'/tmp/x'");
  assert.equal(shellSingleQuote("/tmp/o'brien"), `'/tmp/o'\\''brien'`);
});

test("materializeAgentBody injects absolute agent-run and managed header", () => {
  const src = fs.readFileSync(
    path.join(TEMPLATES, "grok-engineer-coder.toml"),
    "utf8"
  );
  const agentRun = "/cache/grok/1.2.5/agents/run.mjs";
  const companion = "/cache/grok/1.2.5/scripts/grok-companion.mjs";
  const body = materializeAgentBody(src, agentRun, companion);
  assert.ok(isManagedAgentBody(body));
  assert.ok(body.includes(`agent-run: ${agentRun}`));
  assert.ok(body.includes(`GROK_AGENT_RUN='${agentRun}'`));
  assert.ok(!body.includes("__GROK_AGENT_RUN_Q__"));
});

test("installCodexAgents writes managed agents with absolute agent-run", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const env = { CODEX_HOME: home };
  const first = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
  });
  assert.equal(first.ok, true);
  assert.ok(first.installed.includes("grok-engineer-coder"));
  assert.ok(first.installed.includes("grok-rescue"));

  const dest = path.join(home, "agents", "grok-engineer-coder.toml");
  const body = fs.readFileSync(dest, "utf8");
  const agentRun = resolveAgentRunPath(PLUGIN_ROOT);
  const companion = resolveCompanionPath(PLUGIN_ROOT);
  assert.ok(isManagedAgentBody(body));
  assert.ok(body.includes(agentRun));
  assert.ok(body.includes(`GROK_AGENT_RUN=${shellSingleQuote(agentRun)}`));
  assert.ok(fs.existsSync(agentRun));
  assert.ok(fs.existsSync(companion));
});

test("installCodexAgents backs up before updating managed agents", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const env = { CODEX_HOME: home };
  installCodexAgents({ templatesDir: TEMPLATES, env, pluginRoot: PLUGIN_ROOT });

  const fakeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "plugin-root-"));
  const scriptsDir = path.join(fakeRoot, "scripts");
  const agentsDir = path.join(fakeRoot, "agents");
  fs.mkdirSync(scriptsDir, { recursive: true });
  fs.mkdirSync(agentsDir, { recursive: true });
  fs.writeFileSync(path.join(scriptsDir, "grok-companion.mjs"), "// stub\n");
  fs.writeFileSync(path.join(agentsDir, "run.mjs"), "// stub agent run\n");

  const third = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: fakeRoot,
    updateManaged: true,
    backup: true,
  });
  assert.equal(third.ok, true);
  assert.ok(third.updated.includes("grok-engineer-coder"));
  assert.ok(third.backedUp.some((b) => b.includes("grok-engineer-coder")));
  assert.ok(
    fs.existsSync(path.join(home, "agents", "grok-engineer-coder.toml.bak"))
  );
});

test("installCodexAgents does not overwrite unmanaged user agents without force", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const env = { CODEX_HOME: home };
  const agentsDir = path.join(home, "agents");
  fs.mkdirSync(agentsDir, { recursive: true });
  const custom = "# my custom agent\nname = \"grok-engineer-coder\"\n";
  fs.writeFileSync(path.join(agentsDir, "grok-engineer-coder.toml"), custom);

  const result = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
    force: false,
  });
  assert.equal(result.ok, true);
  assert.ok(result.skippedUser.includes("grok-engineer-coder"));
  assert.equal(
    fs.readFileSync(path.join(agentsDir, "grok-engineer-coder.toml"), "utf8"),
    custom
  );

  const forced = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
    force: true,
    backup: true,
  });
  assert.ok(forced.updated.includes("grok-engineer-coder"));
  assert.ok(forced.backedUp.length >= 1);
  assert.ok(
    isManagedAgentBody(
      fs.readFileSync(path.join(agentsDir, "grok-engineer-coder.toml"), "utf8")
    )
  );
});

test("uninstallCodexAgents removes managed only and keeps user-owned", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const env = { CODEX_HOME: home };
  installCodexAgents({ templatesDir: TEMPLATES, env, pluginRoot: PLUGIN_ROOT });

  const agentsDir = path.join(home, "agents");
  fs.writeFileSync(
    path.join(agentsDir, "grok-custom.toml"),
    "# not managed\nname = \"grok-custom\"\n"
  );

  const result = uninstallCodexAgents({ env, backup: true });
  assert.equal(result.ok, true);
  assert.ok(result.removed.includes("grok-engineer-coder"));
  assert.ok(result.removed.includes("grok-rescue"));
  assert.ok(result.skippedUser.includes("grok-custom"));
  assert.ok(!fs.existsSync(path.join(agentsDir, "grok-engineer-coder.toml")));
  assert.ok(fs.existsSync(path.join(agentsDir, "grok-engineer-coder.toml.bak")));
  assert.ok(fs.existsSync(path.join(agentsDir, "grok-custom.toml")));
});

test("ensureCodexAgents never throws", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const result = ensureCodexAgents({
    templatesDir: "/nonexistent-templates-dir",
    env: { CODEX_HOME: home },
    pluginRoot: PLUGIN_ROOT,
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.length > 0);
});

// --- Task 4.1a: project-scoped Codex agents via workspace prefs ---

test("codex agents scope prefs default user and persist project", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "codex-scope-prefs-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(getCodexAgentsScope(cwd, env), "user");
  assert.equal(setCodexAgentsScope(cwd, "project", env), "project");
  assert.equal(getCodexAgentsScope(cwd, env), "project");
  assert.equal(setCodexAgentsScope(cwd, "user", env), "user");
  assert.equal(getCodexAgentsScope(cwd, env), "user");
});

test("prefs project scope resolves dest under cwd/.codex/agents", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "codex-scope-dest-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-scope-"));
  const env = {
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata"),
    CODEX_HOME: home,
  };
  setCodexAgentsScope(cwd, "project", env);
  const dest = resolveCodexAgentsDestDir({ cwd, env });
  assert.equal(dest, path.join(cwd, ".codex", "agents"));
  // user scope still targets personal agents dir
  setCodexAgentsScope(cwd, "user", env);
  assert.equal(resolveCodexAgentsDestDir({ cwd, env }), path.join(home, "agents"));
});

test("installCodexAgents honors project scope prefs for destDir", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "codex-scope-install-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-install-"));
  const env = {
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata"),
    CODEX_HOME: home,
  };
  setCodexAgentsScope(cwd, "project", env);
  const result = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    cwd,
    pluginRoot: PLUGIN_ROOT,
  });
  assert.equal(result.ok, true);
  assert.equal(result.destDir, path.join(cwd, ".codex", "agents"));
  assert.ok(
    fs.existsSync(path.join(cwd, ".codex", "agents", "grok-engineer-coder.toml"))
  );
  assert.ok(
    !fs.existsSync(path.join(home, "agents", "grok-engineer-coder.toml")),
    "project scope must not write personal ~/.codex/agents"
  );
});

// --- Task 4.1b: managed backup cap (keep newest 3 total) ---

test("backupAgentFile caps managed backups at 3 newest including the new one", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-bak-cap-"));
  const target = path.join(dir, "grok-engineer-coder.toml");
  const managedBody =
    "# managed-by: grok-skills\n# agent-run: /x\nname = \"grok-engineer-coder\"\n";
  fs.writeFileSync(target, managedBody);

  // Five existing managed backups with staggered mtimes (oldest -> newest).
  const prior = [];
  for (let i = 0; i < 5; i += 1) {
    const bak =
      i === 0 ? `${target}.bak` : `${target}.bak.${i}`;
    fs.writeFileSync(bak, `${managedBody}# bak-gen ${i}\n`);
    const mtime = new Date(Date.now() - (5 - i) * 60_000);
    fs.utimesSync(bak, mtime, mtime);
    prior.push({ path: bak, mtimeMs: mtime.getTime(), i });
  }

  // User-owned backup must never be pruned.
  const userBak = path.join(dir, "user-custom.toml.bak");
  fs.writeFileSync(userBak, "# not managed\nname = \"user-custom\"\n");
  const userMtime = new Date(Date.now() - 10 * 60_000);
  fs.utimesSync(userBak, userMtime, userMtime);

  // Bump target mtime so the new backup is newest.
  const now = new Date();
  fs.utimesSync(target, now, now);
  const created = backupAgentFile(target);
  assert.ok(created);
  assert.ok(fs.existsSync(created));

  const managedBackups = fs
    .readdirSync(dir)
    .filter(
      (n) =>
        n === "grok-engineer-coder.toml.bak" ||
        /^grok-engineer-coder\.toml\.bak\.\d+$/.test(n)
    )
    .map((n) => {
      const p = path.join(dir, n);
      return { name: n, path: p, mtimeMs: fs.statSync(p).mtimeMs };
    })
    .sort((a, b) => b.mtimeMs - a.mtimeMs);

  assert.equal(
    managedBackups.length,
    3,
    `expected 3 managed backups total (including new), got ${managedBackups.map((b) => b.name).join(", ")}`
  );
  // Newest must be the just-created backup.
  assert.equal(managedBackups[0].path, created);
  // User backup untouched.
  assert.ok(fs.existsSync(userBak));
});

test("backupAgentFile does not prune non-managed backups", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-bak-user-"));
  const target = path.join(dir, "grok-engineer-coder.toml");
  // Current file is managed (so install would backup it), but prior backups are user content.
  const managedBody =
    "# managed-by: grok-skills\nname = \"grok-engineer-coder\"\n";
  fs.writeFileSync(target, managedBody);
  const userBackups = [];
  for (let i = 0; i < 4; i += 1) {
    const bak = i === 0 ? `${target}.bak` : `${target}.bak.${i}`;
    fs.writeFileSync(bak, `# user edit ${i}\nname = \"grok-engineer-coder\"\n`);
    userBackups.push(bak);
  }
  backupAgentFile(target);
  for (const bak of userBackups) {
    assert.ok(fs.existsSync(bak), `user backup must remain: ${bak}`);
  }
});

test("materializeAgentBody keeps nickname_candidates from templates", () => {
  const eng = fs.readFileSync(
    path.join(TEMPLATES, "grok-engineer-coder.toml"),
    "utf8"
  );
  const res = fs.readFileSync(path.join(TEMPLATES, "grok-rescue.toml"), "utf8");
  assert.match(eng, /nickname_candidates\s*=\s*\["Grok Coder"\]/);
  assert.match(res, /nickname_candidates\s*=\s*\["Grok Rescue"\]/);
  const body = materializeAgentBody(eng, "/cache/agents/run.mjs", "/cache/scripts/x.mjs");
  assert.match(body, /nickname_candidates\s*=\s*\["Grok Coder"\]/);
  assert.ok(!body.includes("__GROK_AGENT_RUN_Q__"));
});
