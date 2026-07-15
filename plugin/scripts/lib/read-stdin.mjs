// plugin/scripts/lib/read-stdin.mjs
//
// Synchronously drain ALL of a process's stdin (fd 0) into a Buffer, tolerating a
// NON-blocking pipe. Node hands a piped stdin to a synchronous reader in
// non-blocking mode, so a plain fs.readFileSync(0) throws EAGAIN as soon as the
// pipe momentarily has no data ready -- dropping the rest of a large payload. That
// silently truncates exactly the large inputs the Grok stop gate must handle: the
// harness's Stop-hook JSON (a big last_assistant_message) and the review task the
// gate then pipes to the companion. Both hops route through this helper.
//
// The loop reads in 64 KiB chunks; on EAGAIN (no data ready yet) it waits ~1ms via
// Atomics.wait -- a real wait on the main thread, never a busy spin -- and retries,
// exactly mirroring a blocking read; it stops on EOF (readSync returns 0, or throws
// code "EOF" on platforms that surface end-of-input that way). Every other error
// propagates so callers can fail closed.

import fs from "node:fs";

const CHUNK_SIZE = 1 << 16;

/**
 * Drain fd 0 to EOF and return the raw bytes. Blocks (via ~1ms naps) across EAGAIN
 * on a non-blocking pipe until end-of-input; propagates any non-EAGAIN read error.
 *
 * @returns {Buffer}
 */
export function readAllStdinSync() {
  const chunk = Buffer.allocUnsafe(CHUNK_SIZE);
  const collected = [];
  const napHandle = new Int32Array(new SharedArrayBuffer(4));
  for (;;) {
    let bytesRead;
    try {
      bytesRead = fs.readSync(0, chunk, 0, CHUNK_SIZE, null);
    } catch (err) {
      if (err && err.code === "EAGAIN") {
        // Non-blocking stdin with no data ready yet: wait ~1ms, then retry.
        Atomics.wait(napHandle, 0, 0, 1);
        continue;
      }
      if (err && err.code === "EOF") {
        break;
      }
      throw err;
    }
    if (bytesRead === 0) {
      break;
    }
    collected.push(Buffer.from(chunk.subarray(0, bytesRead)));
  }
  return Buffer.concat(collected);
}
