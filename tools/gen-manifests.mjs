#!/usr/bin/env node
// tools/gen-manifests.mjs
//
// Single-source generator for dual-host plugin manifests and BOTH marketplace
// roots. Reads plugin/manifest.source.json and WRITES:
//   - plugin/.claude-plugin/plugin.json
//   - plugin/.codex-plugin/plugin.json
//   - .claude-plugin/marketplace.json  (Claude marketplace root)
//   - .agents/plugins/marketplace.json (Codex marketplace root)
//
// For the marketplace roots the generator sources every shared FACT that would
// otherwise drift by hand - version (where the schema carries one), the per-host
// rendered description, keywords, and displayName - while preserving each root's
// host-specific structure/key order. Both roots are guarded so Codex marketplace
// metadata cannot go stale while the check passes.
//
// Deterministic key order, 2-space indent, trailing newline. --check exits 1 on
// drift without writing (CI / pre-commit / tools/checks.sh). Install stays
// build-free: generated files remain committed.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// GEN_MANIFESTS_ROOT lets tests run against an isolated temp copy of the repo
// so a drift check never mutates the real committed manifests (concurrency-safe).
const REPO_ROOT = process.env.GEN_MANIFESTS_ROOT
  ? path.resolve(process.env.GEN_MANIFESTS_ROOT)
  : path.resolve(HERE, "..");

const SOURCE_REL = "plugin/manifest.source.json";
const CLAUDE_PLUGIN_REL = "plugin/.claude-plugin/plugin.json";
const CODEX_PLUGIN_REL = "plugin/.codex-plugin/plugin.json";
const MARKETPLACE_REL = ".claude-plugin/marketplace.json";
const CODEX_MARKETPLACE_REL = ".agents/plugins/marketplace.json";

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
 * Claude marketplace root: source version, per-host description, and keywords
 * from manifest.source.json; preserve everything else (owner, source path,
 * license, marketplace name) and the committed key order.
 */
function buildClaudeMarketplace(existing, src) {
  const next = structuredClone(existing);
  const description = renderDescription(src.descriptionTemplate, src.claude.hostName);
  if (!next.metadata || typeof next.metadata !== "object") {
    throw new Error(`${MARKETPLACE_REL} missing metadata object`);
  }
  next.metadata.version = src.version;
  next.metadata.description = description;
  if (!Array.isArray(next.plugins)) {
    throw new Error(`${MARKETPLACE_REL} missing plugins array`);
  }
  for (const entry of next.plugins) {
    entry.version = src.version;
    entry.description = description;
    entry.keywords = [...src.keywords];
  }
  return next;
}

/**
 * Codex marketplace root (.agents): no version/keywords in this schema, so
 * source the drift-prone facts it DOES carry - displayName and the per-host
 * rendered description - and preserve its host-specific structure (source
 * object, policy, category, icon) and key order.
 */
function buildCodexMarketplace(existing, src) {
  const next = structuredClone(existing);
  const description = renderDescription(src.descriptionTemplate, src.codex.hostName);
  if (!next.interface || typeof next.interface !== "object") {
    throw new Error(`${CODEX_MARKETPLACE_REL} missing interface object`);
  }
  next.interface.displayName = src.displayName;
  if (!Array.isArray(next.plugins)) {
    throw new Error(`${CODEX_MARKETPLACE_REL} missing plugins array`);
  }
  for (const entry of next.plugins) {
    entry.displayName = src.displayName;
    entry.description = description;
  }
  return next;
}

function plannedOutputs(src) {
  const marketplacePath = path.join(REPO_ROOT, MARKETPLACE_REL);
  const codexMarketplacePath = path.join(REPO_ROOT, CODEX_MARKETPLACE_REL);
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
      body: formatJson(buildClaudeMarketplace(readJson(marketplacePath), src)),
    },
    {
      rel: CODEX_MARKETPLACE_REL,
      abs: codexMarketplacePath,
      body: formatJson(buildCodexMarketplace(readJson(codexMarketplacePath), src)),
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
