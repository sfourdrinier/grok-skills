// plugin/scripts/tests/git-c-quoted-path-vectors.test.mjs
//
// Shared golden-vector parity guard for Node unquoteGitPath (and numstat path
// decode). Loads plugin/references/git-c-quoted-path-vectors.json - the same
// SSOT file Python tests read. No runtime cross-language dependency.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { parseNumstatPaths, unquoteGitPath } from "../lib/integrate.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VECTORS_PATH = path.resolve(
  HERE,
  "../../references/git-c-quoted-path-vectors.json"
);

function loadVectors() {
  const raw = fs.readFileSync(VECTORS_PATH, "utf8");
  return JSON.parse(raw);
}

test("shared golden vectors file exists and is the path-quote SSOT", () => {
  assert.ok(fs.existsSync(VECTORS_PATH), `missing vectors at ${VECTORS_PATH}`);
  const v = loadVectors();
  assert.equal(v.schemaVersion, 1);
  assert.ok(Array.isArray(v.tokenDecode) && v.tokenDecode.length > 0);
});

test("unquoteGitPath matches shared golden tokenDecode vectors (node)", () => {
  const v = loadVectors();
  const cases = v.tokenDecode.filter(
    (c) => !c.appliesTo || c.appliesTo.includes("node")
  );
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

test("parseNumstatPaths uses the same C-quote decode for quoted path fields", () => {
  // UTF-8 octal: "é.txt" as git would emit on --numstat
  assert.deepEqual(parseNumstatPaths('1\t0\t"\\303\\251.txt"\n'), ["é.txt"]);
  assert.deepEqual(parseNumstatPaths('0\t0\t"quote\\"here.txt"\n'), ['quote"here.txt']);
  assert.deepEqual(parseNumstatPaths("1\t0\tplain.js\n"), ["plain.js"]);
});
