#!/usr/bin/env node
// tools/gen-manifests.mjs
//
// Single-source generator for dual-host plugin manifests and Claude marketplace
// version fields. Reads plugin/manifest.source.json and WRITES:
//   - plugin/.claude-plugin/plugin.json
//   - plugin/.codex-plugin/plugin.json
//   - .claude-plugin/marketplace.json (metadata.version + plugins[].version only)
//
// Deterministic key order, 2-space indent, trailing newline. --check exits 1 on
// drift without writing (CI / pre-commit / tools/checks.sh). Install stays
// build-free: generated files remain committed.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..");

const SOURCE_REL = "plugin/manifest.source.json";
const CLAUDE_PLUGIN_REL = "plugin/.claude-plugin/plugin.json";
const CODEX_PLUGIN_REL = "plugin/.codex-plugin/plugin.json";
const MARKETPLACE_REL = ".claude-plugin/marketplace.json";

function readJson(abs) {
  return JSON.parse(fs.readFileSync(abs, "utf8"));
}

function formatJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function renderDescription(template, hostName) {
  if (typeof template !== "string" || !template.includes("{{host}}")) {
    throw new Error(
      "manifest.source.json descriptionTemplate must contain {{host}} placeholder"
    );
  }
  return template.replaceAll("{{host}}", hostName);
}

/**
 * Insert codex-only keyword after a shared anchor so keyword order stays
 * byte-stable with the historical hand-maintained Codex manifest.
 */
function codexKeywords(shared, extraKeyword, after) {
  const out = [];
  let inserted = false;
  for (const kw of shared) {
    out.push(kw);
    if (kw === after) {
      out.push(extraKeyword);
      inserted = true;
    }
  }
  if (!inserted) {
    throw new Error(
      `codex.extraKeywordAfter "${after}" not found in shared keywords`
    );
  }
  return out;
}

function buildClaudeManifest(src) {
  // Key order matches the committed Claude plugin.json (byte-faithful).
  return {
    name: src.name,
    version: src.version,
    description: renderDescription(src.descriptionTemplate, src.claude.hostName),
    author: src.author,
    homepage: src.homepage,
    repository: src.repository,
    license: src.license,
    keywords: [...src.keywords],
    displayName: src.displayName,
    userConfig: src.claude.userConfig,
  };
}

function buildCodexManifest(src) {
  // Key order matches the committed Codex plugin.json (byte-faithful).
  return {
    name: src.name,
    version: src.version,
    description: renderDescription(src.descriptionTemplate, src.codex.hostName),
    author: src.author,
    homepage: src.homepage,
    repository: src.repository,
    license: src.license,
    keywords: codexKeywords(
      src.keywords,
      src.codex.extraKeyword,
      src.codex.extraKeywordAfter
    ),
    skills: src.codex.skills,
    hooks: src.codex.hooks,
    interface: src.codex.interface,
  };
}

/**
 * Only version fields are generated for marketplace; everything else is
 * preserved from the committed file so non-version metadata stays hand-owned.
 */
function buildMarketplace(existing, version) {
  const next = structuredClone(existing);
  if (!next.metadata || typeof next.metadata !== "object") {
    throw new Error("marketplace.json missing metadata object");
  }
  next.metadata.version = version;
  if (!Array.isArray(next.plugins)) {
    throw new Error("marketplace.json missing plugins array");
  }
  for (const entry of next.plugins) {
    entry.version = version;
  }
  return next;
}

function plannedOutputs(src) {
  const marketplacePath = path.join(REPO_ROOT, MARKETPLACE_REL);
  const marketplaceExisting = readJson(marketplacePath);
  return [
    {
      rel: CLAUDE_PLUGIN_REL,
      abs: path.join(REPO_ROOT, CLAUDE_PLUGIN_REL),
      body: formatJson(buildClaudeManifest(src)),
    },
    {
      rel: CODEX_PLUGIN_REL,
      abs: path.join(REPO_ROOT, CODEX_PLUGIN_REL),
      body: formatJson(buildCodexManifest(src)),
    },
    {
      rel: MARKETPLACE_REL,
      abs: marketplacePath,
      body: formatJson(buildMarketplace(marketplaceExisting, src.version)),
    },
  ];
}

function main(argv = process.argv.slice(2)) {
  const checkOnly = argv.includes("--check");
  const sourcePath = path.join(REPO_ROOT, SOURCE_REL);
  if (!fs.existsSync(sourcePath)) {
    console.error(`gen-manifests: missing source ${SOURCE_REL}`);
    process.exit(1);
  }
  const src = readJson(sourcePath);
  if (!src.version || !src.claude || !src.codex) {
    console.error("gen-manifests: source missing version/claude/codex");
    process.exit(1);
  }

  const outputs = plannedOutputs(src);
  let drift = false;

  for (const out of outputs) {
    const onDisk = fs.existsSync(out.abs) ? fs.readFileSync(out.abs, "utf8") : null;
    if (onDisk === out.body) {
      continue;
    }
    drift = true;
    if (checkOnly) {
      console.error(`gen-manifests --check: drift in ${out.rel}`);
      continue;
    }
    fs.mkdirSync(path.dirname(out.abs), { recursive: true });
    fs.writeFileSync(out.abs, out.body, "utf8");
    console.error(`gen-manifests: wrote ${out.rel}`);
  }

  if (checkOnly) {
    if (drift) {
      console.error(
        "gen-manifests --check: FAIL (run: node tools/gen-manifests.mjs)"
      );
      process.exit(1);
    }
    console.error("gen-manifests --check: OK");
    process.exit(0);
  }

  if (!drift) {
    console.error("gen-manifests: already up to date");
  }
  process.exit(0);
}

main();
