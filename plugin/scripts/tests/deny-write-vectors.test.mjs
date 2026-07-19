// plugin/scripts/tests/deny-write-vectors.test.mjs
//
// Shared golden-vector parity guard for Node pathMatchesDeny. Loads the same
// plugin/references/deny-write-globs.json Python tests read.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  DENY_WRITE_SSOT_PATH,
  denyWriteGlobs,
  loadDenyWriteSsot,
  pathMatchesDeny,
  protectedPathsIn,
} from "../lib/deny-write.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXPECTED_SSOT = path.resolve(HERE, "../../references/deny-write-globs.json");

test("deny-write SSOT exists and is the path under plugin/references", () => {
  assert.equal(DENY_WRITE_SSOT_PATH, EXPECTED_SSOT);
  assert.ok(fs.existsSync(DENY_WRITE_SSOT_PATH), `missing ${DENY_WRITE_SSOT_PATH}`);
  const doc = loadDenyWriteSsot();
  assert.equal(doc.schemaVersion, 1);
  assert.ok(Array.isArray(doc.globs) && doc.globs.length >= 10);
  assert.ok(doc.globs.includes(".env"));
  assert.ok(doc.globs.includes("credentials.json"));
  assert.deepEqual(denyWriteGlobs(), doc.globs);
});

test("pathMatchesDeny matches shared golden matchVectors (node)", () => {
  const doc = loadDenyWriteSsot();
  const cases = (doc.matchVectors || []).filter(
    (c) => !c.appliesTo || c.appliesTo.includes("node")
  );
  assert.ok(cases.length >= 20, "expected a useful set of node match vectors");
  for (const c of cases) {
    const got = pathMatchesDeny(c.path);
    assert.equal(
      got,
      c.expected,
      `vector ${c.id}: path=${JSON.stringify(c.path)} got=${got} expected=${c.expected}`
    );
  }
});

test("protectedPathsIn returns sorted deny-matched offenders only", () => {
  assert.deepEqual(
    protectedPathsIn(["src/app.py", ".env", "README.md", ".git/index", "ok.ts"]),
    [".env", ".git/index"]
  );
  assert.deepEqual(protectedPathsIn(["src/a.ts", "lib/b.js"]), []);
});

test("required cases from task: env, git hooks/index, keys, credentials", () => {
  for (const p of [
    ".env",
    ".env.local",
    ".git/hooks/vendor/pre-commit",
    ".git/index",
    "id_rsa",
    "keys/id_ed25519",
    "server.pem",
    "credentials.json",
  ]) {
    assert.equal(pathMatchesDeny(p), true, p);
  }
  for (const p of ["src/app.py", "package.json", "env.local"]) {
    assert.equal(pathMatchesDeny(p), false, p);
  }
});
