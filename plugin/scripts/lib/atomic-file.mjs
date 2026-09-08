// plugin/scripts/lib/atomic-file.mjs
//
// Exclusive mkdir lock + temp+rename publish. Node counterpart of
// groklib.filelock + atomic replace. Stdlib only.

import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const DIR_MODE = 0o700;
export const FILE_MODE = 0o600;
export const LOCK_STALE_MS = 30_000;
export const LOCK_WAIT_MS = 5_000;

export function mkdirPrivate(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: DIR_MODE });
  try {
    fs.chmodSync(dir, DIR_MODE);
  } catch {
    /* best-effort on platforms without chmod */
  }
}

function sleepSync(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      /* spin */
    }
  }
}

export function atomicWritePrivate(filePath, content) {
  const dir = path.dirname(filePath);
  mkdirPrivate(dir);
  const tmp = path.join(
    dir,
    `.${path.basename(filePath)}.tmp-${process.pid}-${randomBytes(4).toString("hex")}`
  );
  try {
    fs.writeFileSync(tmp, content, { encoding: "utf8", mode: FILE_MODE });
    try {
      fs.chmodSync(tmp, FILE_MODE);
    } catch {
      /* best-effort */
    }
    fs.renameSync(tmp, filePath);
  } catch (err) {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* best-effort cleanup */
    }
    throw err;
  }
}

function lockAgeMs(lockDir) {
  try {
    const st = fs.statSync(lockDir);
    return Date.now() - st.mtimeMs;
  } catch {
    return 0;
  }
}

export function withExclusiveLockSync(lockDir, fn, opts = {}) {
  const waitMs = opts.waitMs ?? LOCK_WAIT_MS;
  const staleMs = opts.staleMs ?? LOCK_STALE_MS;
  const start = Date.now();
  while (true) {
    try {
      fs.mkdirSync(lockDir, { mode: DIR_MODE });
      break;
    } catch (err) {
      if (!err || err.code !== "EEXIST") throw err;
      if (lockAgeMs(lockDir) > staleMs) {
        try {
          fs.rmdirSync(lockDir);
          continue;
        } catch {
          /* raced */
        }
      }
      if (Date.now() - start > waitMs) {
        throw new Error(`exclusive lock timeout: ${lockDir}`);
      }
      sleepSync(20);
    }
  }
  try {
    return fn();
  } finally {
    try {
      fs.rmdirSync(lockDir);
    } catch {
      /* lock dir already gone */
    }
  }
}
