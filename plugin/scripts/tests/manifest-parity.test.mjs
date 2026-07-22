// plugin/scripts/tests/manifest-parity.test.mjs
//
// Task 6.1: dual-manifest drift guard - Claude/Codex parity, packaging
// version surfaces from RELEASE.md, and tolerant CHANGELOG heading check.
// Task 7.6: also asserts tools/gen-manifests.mjs --check on the committed tree
// (independent of the generator's own write path; keeps the guard if someone
// hand-edits a generated file).
// Reads live files from the repo root (no fixtures).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// tests/ -> scripts/ -> plugin/ -> repo root
const REPO_ROOT = path.resolve(HERE, "../../..");

const CLAUDE_PLUGIN = "plugin/.claude-plugin/plugin.json";
const CODEX_PLUGIN = "plugin/.codex-plugin/plugin.json";
const CLAUDE_MARKETPLACE = ".claude-plugin/marketplace.json";
const CODEX_MARKETPLACE = ".agents/plugins/marketplace.json";
const CHANGELOG = "CHANGELOG.md";
const GEN_MANIFESTS = path.join(REPO_ROOT, "tools", "gen-manifests.mjs");

const SHARED_FIELDS = ["name", "version", "license", "homepage", "repository"];
const CODEX_ONLY_KEYWORD = "claude-code";
const UNRELEASED_HEADING = "## [2.0.0] - unreleased";

function readJson(rel) {
  const abs = path.join(REPO_ROOT, rel);
  return JSON.parse(fs.readFileSync(abs, "utf8"));
}

function readText(rel) {
  return fs.readFileSync(path.join(REPO_ROOT, rel), "utf8");
}

function keywordSet(keywords, dropExtra) {
  const set = new Set(keywords);
  if (dropExtra) set.delete(CODEX_ONLY_KEYWORD);
  return set;
}

function setsEqual(a, b) {
  if (a.size !== b.size) return false;
  for (const item of a) {
    if (!b.has(item)) return false;
  }
  return true;
}

test("Claude and Codex plugin.json agree on shared identity fields and keyword sets", () => {
  const claude = readJson(CLAUDE_PLUGIN);
  const codex = readJson(CODEX_PLUGIN);

  for (const field of SHARED_FIELDS) {
    assert.equal(
      claude[field],
      codex[field],
      `${field} must match across Claude and Codex manifests`
    );
  }
  assert.equal(
    claude.author?.name,
    codex.author?.name,
    "author.name must match across Claude and Codex manifests"
  );

  const claudeKw = keywordSet(claude.keywords, false);
  const codexKw = keywordSet(codex.keywords, true);
  assert.ok(
    setsEqual(claudeKw, codexKw),
    `keyword sets must match after removing codex-only "${CODEX_ONLY_KEYWORD}"; ` +
      `claude=${[...claudeKw].sort().join(",")} codex=${[...codexKw].sort().join(",")}`
  );
  assert.ok(
    codex.keywords.includes(CODEX_ONLY_KEYWORD),
    `Codex keywords must keep the host-extra "${CODEX_ONLY_KEYWORD}"`
  );
});

test("packaging versions match RELEASE.md surfaces (plugin + Claude marketplace)", () => {
  const claude = readJson(CLAUDE_PLUGIN);
  const codex = readJson(CODEX_PLUGIN);
  const marketplace = readJson(CLAUDE_MARKETPLACE);

  const version = claude.version;
  assert.equal(typeof version, "string");
  assert.ok(version.length > 0, "Claude plugin version must be non-empty");

  // RELEASE.md: plugin/.claude-plugin/plugin.json version
  // equals plugin/.codex-plugin/plugin.json version
  assert.equal(codex.version, version, "Codex plugin.json version must match Claude");

  // RELEASE.md: .claude-plugin/marketplace.json metadata.version
  assert.equal(
    marketplace.metadata?.version,
    version,
    "marketplace metadata.version must match Claude plugin version"
  );

  // RELEASE.md: each plugins[].version
  assert.ok(
    Array.isArray(marketplace.plugins) && marketplace.plugins.length > 0,
    "marketplace.plugins must be a non-empty array"
  );
  for (const [i, entry] of marketplace.plugins.entries()) {
    assert.equal(
      entry.version,
      version,
      `marketplace.plugins[${i}].version must match Claude plugin version`
    );
  }

  // .agents/plugins/marketplace.json has NO version field - do not assert one.
});

test("both marketplace roots source description/keywords from the single manifest source", () => {
  const claudePlugin = readJson(CLAUDE_PLUGIN);
  const codexPlugin = readJson(CODEX_PLUGIN);
  const claudeMkt = readJson(CLAUDE_MARKETPLACE);
  const codexMkt = readJson(CODEX_MARKETPLACE);

  // Marketplace descriptions are the generated per-host plugin descriptions, so
  // they cannot drift by hand (the pre-2.0 wording gap that survived the old
  // version-only guard).
  assert.equal(
    claudeMkt.metadata?.description,
    claudePlugin.description,
    "Claude marketplace metadata.description must equal the generated Claude plugin description"
  );
  assert.equal(
    claudeMkt.plugins?.[0]?.description,
    claudePlugin.description,
    "Claude marketplace plugin description must equal the generated Claude plugin description"
  );
  assert.equal(
    codexMkt.plugins?.[0]?.description,
    codexPlugin.description,
    "Codex marketplace plugin description must equal the generated Codex plugin description"
  );

  assert.ok(
    setsEqual(
      new Set(claudeMkt.plugins?.[0]?.keywords),
      new Set(claudePlugin.keywords)
    ),
    "Claude marketplace keywords must match the Claude plugin keyword set"
  );

  // Regression guard: none of the retired hand-wording may reappear in either root.
  const blob = JSON.stringify(claudeMkt) + JSON.stringify(codexMkt);
  for (const stale of [
    "coding assistant from Claude Code",
    "self-contained sandboxed wrapper",
    "Hardened Grok companion",
    // Phase 7 peer-native: marketplace must not claim isolated-worktree-only landing.
    "delegate code to an isolated worktree",
    "Delegate code into isolated git worktrees",
  ]) {
    assert.ok(!blob.includes(stale), `retired marketplace wording must be gone: "${stale}"`);
  }

  // Phase 7 honesty: product default is consented live-tree edit landing; auto/review
  // are opt-in worktrees; ACP is the default peer channel; runMode direct is separate.
  for (const desc of [
    claudePlugin.description,
    codexPlugin.description,
    claudeMkt.metadata?.description,
    claudeMkt.plugins?.[0]?.description,
    codexMkt.plugins?.[0]?.description,
  ]) {
    assert.equal(typeof desc, "string");
    assert.match(
      desc,
      /integration=direct|live-tree|this tree|working tree/i,
      `description must state integration=direct / live-tree default: ${desc}`
    );
    assert.match(
      desc,
      /auto|review/,
      `description must mention opt-in auto/review worktrees: ${desc}`
    );
    assert.match(desc, /\bACP\b/, `description must name ACP as peer channel: ${desc}`);
  }
  const codexLong = codexPlugin.interface?.longDescription || "";
  assert.match(
    codexLong,
    /integration=direct|live-tree|this tree|working tree/i,
    "Codex longDescription must state consented live-tree default"
  );
  assert.match(codexLong, /\bACP\b/, "Codex longDescription must name ACP");
  assert.match(
    codexLong,
    /runMode|run mode|installed Grok|installed CLI/i,
    "Codex longDescription must separate runMode direct (installed-home posture)"
  );
});

test("gen-manifests --check exits 1 when the Codex marketplace root drifts", () => {
  // The old guard only covered the Claude marketplace root; the Codex root
  // (.agents/plugins/marketplace.json) could go stale while --check passed.
  // This proves it is now guarded.
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "gen-manifests-codex-drift-"));
  try {
    for (const rel of [
      "plugin/manifest.source.json",
      CLAUDE_PLUGIN,
      CODEX_PLUGIN,
      CLAUDE_MARKETPLACE,
      CODEX_MARKETPLACE,
    ]) {
      const dst = path.join(tmpRoot, rel);
      fs.mkdirSync(path.dirname(dst), { recursive: true });
      fs.copyFileSync(path.join(REPO_ROOT, rel), dst);
    }
    const codexCopy = path.join(tmpRoot, CODEX_MARKETPLACE);
    const original = fs.readFileSync(codexCopy, "utf8");
    fs.writeFileSync(
      codexCopy,
      original.replace(/"description": "[^"]*"/, '"description": "stale hand-wording"'),
      "utf8"
    );
    const result = spawnSync(process.execPath, [GEN_MANIFESTS, "--check"], {
      encoding: "utf8",
      env: { ...process.env, GEN_MANIFESTS_ROOT: tmpRoot },
    });
    assert.equal(result.status, 1, "codex marketplace drift must exit 1");
    assert.match(result.stderr, /\.agents\/plugins\/marketplace\.json/);
  } finally {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  }
});

test("CHANGELOG has a section for the manifest version or the 2.0.0 unreleased heading", () => {
  const claude = readJson(CLAUDE_PLUGIN);
  const version = claude.version;
  const changelog = readText(CHANGELOG);

  // Match "## [X.Y.Z]" with optional trailing " - ..." (date or unreleased).
  const versionHeadingRe = new RegExp(
    `^## \\[${version.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\]`,
    "m"
  );
  const hasVersionHeading = versionHeadingRe.test(changelog);
  const hasUnreleasedHeading = changelog.includes(UNRELEASED_HEADING);

  assert.ok(
    hasVersionHeading || hasUnreleasedHeading,
    `CHANGELOG.md must contain "## [${version}]" (any suffix) or the literal ` +
      `"${UNRELEASED_HEADING}" (branch unreleased-tracked state)`
  );
});

test("gen-manifests --check passes on the committed tree (no drift)", () => {
  const result = spawnSync(process.execPath, [GEN_MANIFESTS, "--check"], {
    encoding: "utf8",
    cwd: REPO_ROOT,
  });
  assert.equal(
    result.status,
    0,
    `gen-manifests --check must pass on committed manifests; stdout=${result.stdout} stderr=${result.stderr}`
  );
});

test("gen-manifests --check exits 1 when a generated file drifts", () => {
  // Isolate: copy the source + generated files into a temp root and mutate the
  // COPY, never the committed repo files. Concurrency-safe (two suites in the
  // same checkout no longer race on the real manifests). GEN_MANIFESTS_ROOT
  // points the generator at the temp tree.
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "gen-manifests-drift-"));
  try {
    for (const rel of [
      "plugin/manifest.source.json",
      CLAUDE_PLUGIN,
      CODEX_PLUGIN,
      CLAUDE_MARKETPLACE,
      CODEX_MARKETPLACE,
    ]) {
      const dst = path.join(tmpRoot, rel);
      fs.mkdirSync(path.dirname(dst), { recursive: true });
      fs.copyFileSync(path.join(REPO_ROOT, rel), dst);
    }
    // Mutate the COPY of a generated file so --check must fail closed.
    // Use whatever version is currently in the source (do not hardcode X.Y.Z).
    const claudeCopy = path.join(tmpRoot, CLAUDE_PLUGIN);
    const original = fs.readFileSync(claudeCopy, "utf8");
    const versionMatch = original.match(/"version":\s*"([^"]+)"/);
    assert.ok(versionMatch, "generated plugin.json must contain a version field");
    fs.writeFileSync(
      claudeCopy,
      original.replace(
        `"version": "${versionMatch[1]}"`,
        '"version": "0.0.0-drift"'
      ),
      "utf8"
    );
    const result = spawnSync(process.execPath, [GEN_MANIFESTS, "--check"], {
      encoding: "utf8",
      env: { ...process.env, GEN_MANIFESTS_ROOT: tmpRoot },
    });
    assert.equal(result.status, 1, "drift must exit 1");
    assert.match(result.stderr, /drift/i);
  } finally {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  }
});
