// plugin/scripts/tests/git-c-quoted-path-vectors.test.mjs
//
// Shared golden-vector parity guard for Node unquoteGitPath (and numstat path
// decode). Loads plugin/references/git-c-quoted-path-vectors.json - the same
// SSOT file Python tests read. No runtime cross-language dependency.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  loadPatchTouchPaths,
  parseDiffGitHeaderPaths,
  parseNumstatPaths,
  pathsFromGitPatch,
  unquoteGitPath,
} from "../lib/integrate.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VECTORS_PATH = path.resolve(
  HERE,
  "../../references/git-c-quoted-path-vectors.json"
);

function loadVectors() {
  const raw = fs.readFileSync(VECTORS_PATH, "utf8");
  return JSON.parse(raw);
}

function nodeCases(list) {
  return (list || []).filter((c) => !c.appliesTo || c.appliesTo.includes("node"));
}

test("shared golden vectors file exists and is the path-quote SSOT", () => {
  assert.ok(fs.existsSync(VECTORS_PATH), `missing vectors at ${VECTORS_PATH}`);
  const v = loadVectors();
  assert.equal(v.schemaVersion, 1);
  assert.ok(Array.isArray(v.tokenDecode) && v.tokenDecode.length > 0);
  assert.ok(
    nodeCases(v.diffGitHeaders).some((c) => c.id === "unquoted-literal-space-b-slash-same-path"),
    "diffGitHeaders dual-condition vectors must apply to node"
  );
});

test("unquoteGitPath matches shared golden tokenDecode vectors (node)", () => {
  const v = loadVectors();
  const cases = nodeCases(v.tokenDecode);
  assert.ok(cases.length >= 8, "expected a useful set of node token vectors");
  for (const c of cases) {
    const got = unquoteGitPath(c.input);
    assert.equal(
      got,
      c.expected,
      `vector ${c.id}: input=${JSON.stringify(c.input)} got=${JSON.stringify(got)} expected=${JSON.stringify(c.expected)}`
    );
  }
});

test("parseDiffGitHeaderPaths matches shared dual-condition golden vectors (node)", () => {
  const v = loadVectors();
  const cases = nodeCases(v.diffGitHeaders);
  assert.ok(cases.length >= 5, "expected shared dual-condition header vectors for node");
  for (const c of cases) {
    const got = parseDiffGitHeaderPaths(c.rest);
    if (c.expected == null) {
      assert.equal(got, null, `vector ${c.id} must fail closed`);
    } else {
      assert.deepEqual(
        got,
        c.expected,
        `vector ${c.id}: rest=${JSON.stringify(c.rest)} got=${JSON.stringify(got)} expected=${JSON.stringify(c.expected)}`
      );
    }
  }
});

test("malformedFailClosed golden vectors fail closed on node", () => {
  const v = loadVectors();
  for (const c of nodeCases(v.malformedFailClosed)) {
    assert.equal(parseDiffGitHeaderPaths(c.rest), null, `vector ${c.id}`);
  }
});

test("devNullExclusion golden vectors on node", () => {
  const v = loadVectors();
  for (const c of nodeCases(v.devNullExclusion)) {
    if (c.patchText) {
      assert.deepEqual([...pathsFromGitPatch(c.patchText)].sort(), [...c.expectedPaths].sort());
    } else if (c.rest) {
      const raw = parseDiffGitHeaderPaths(c.rest);
      assert.deepEqual(raw, c.expectedRaw);
      const filtered = [];
      const seen = new Set();
      for (const p of raw) {
        if (p && p !== "/dev/null" && !seen.has(p)) {
          seen.add(p);
          filtered.push(p);
        }
      }
      assert.deepEqual(filtered, c.expectedFiltered);
    }
  }
});

test("parseNumstatPaths uses the same C-quote decode for quoted path fields", () => {
  // UTF-8 octal: "é.txt" as git would emit on --numstat
  assert.deepEqual(parseNumstatPaths('1\t0\t"\\303\\251.txt"\n'), ["é.txt"]);
  assert.deepEqual(parseNumstatPaths('0\t0\t"quote\\"here.txt"\n'), ['quote"here.txt']);
  assert.deepEqual(parseNumstatPaths("1\t0\tplain.js\n"), ["plain.js"]);
});

test("loadPatchTouchPaths keeps literal space-b-slash path (empty or non-empty numstat)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-touch-bslash-"));
  try {
    const patch = path.join(root, "x-b-y.patch");
    fs.writeFileSync(
      patch,
      [
        "diff --git a/x b/y.txt b/x b/y.txt",
        "--- a/x b/y.txt",
        "+++ b/x b/y.txt",
        "@@ -0,0 +1 @@",
        "+payload",
        "",
      ].join("\n")
    );
    const empty = loadPatchTouchPaths(patch, "");
    assert.equal(empty.ok, true, JSON.stringify(empty));
    assert.ok(empty.paths.includes("x b/y.txt"), empty.paths);
    assert.ok(!empty.paths.includes("x"), `must not first-sep mis-split: ${empty.paths}`);

    const withNum = loadPatchTouchPaths(patch, "1\t0\tx b/y.txt\n");
    assert.equal(withNum.ok, true, JSON.stringify(withNum));
    assert.ok(withNum.paths.includes("x b/y.txt"), withNum.paths);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("loadPatchTouchPaths fails closed on ambiguous space-b-slash rename (empty numstat)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-touch-ambig-"));
  try {
    const patch = path.join(root, "ambig.patch");
    fs.writeFileSync(
      patch,
      [
        "diff --git a/x b/y.txt b/z b/w.txt",
        "--- a/x b/y.txt",
        "+++ b/z b/w.txt",
        "@@ -0,0 +1 @@",
        "+payload",
        "",
      ].join("\n")
    );
    const empty = loadPatchTouchPaths(patch, "");
    assert.equal(empty.ok, false, "empty numstat must not ship wrong header-only touch paths");
    assert.equal(empty.outcome, "blocked-patch-headers");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
