<!-- plugin/wrapper/scripts/tests/live/README.md -->

# Live read-only probe suite (Task 13)

`live_probes.py` is a MANUALLY invoked script, NOT a unittest module. It drives
the finished wrapper (`scripts/grok_agent.py`) end to end against the REAL Grok
CLI (the operator's real `~/.grok` auth material and `~/.grok/bin/grok` binary)
and asserts on the returned C4 envelopes. Maintainers may use `--revalidate` to
refresh the **advisory** last-validated stamp in `accepted-version.json`
(`enforcement: none`). Runtime never hard-pins a CLI build.

It lives under `tests/live/` with a non-`test_*` filename precisely so the unit
suite never runs it:

```bash
# from scripts/ -- live_probes.py is NOT discovered (no test_* name)
python3 -m unittest discover -s tests -t .
```

## Why it is separate from the unit suite

The unit suite (`tests/test_*.py`) is hermetic: it mocks the Grok binary via
`tests/fake_grok.py` and never touches the network, real auth, or real model
turns. This live suite is the opposite: it makes real, authenticated, minutes-
long model calls. It must never run in CI-style automation or be auto-discovered
by `unittest`; it is run by hand, deliberately, on a machine that is logged in to
Grok.

## Running it

```bash
# Run every probe, print a summary, exit 0 iff every GATING probe passed.
# Does NOT touch the last-validated stamp.
python3 plugin/wrapper/scripts/tests/live/live_probes.py

# Also dump the machine-readable evidence JSON somewhere.
python3 plugin/wrapper/scripts/tests/live/live_probes.py \
  --evidence-out /tmp/grok-live-evidence.json

# Refresh the advisory stamp: re-run the full suite and, ONLY on a fully green
# gating run, rewrite accepted-version.json with the installed version, a fresh
# UTC timestamp, evidence pointer, and enforcement: none. A red run leaves the
# stamp untouched. Users are never blocked on stamp mismatch.
python3 plugin/wrapper/scripts/tests/live/live_probes.py --revalidate
```

The suite takes several minutes: each gating probe makes at least one live model
call. Give it a generous outer timeout and do not kill it on a short stall; a
real hang is caught by the wrapper's own inner timeout long before the outer
guard fires.

## What it probes

Gating probes (their collective pass/fail is the suite's exit code and the only
thing `--revalidate` keys off):

1. `preflight` -- success, runnable Grok CLI (any working build),
   `secretReadDenial:false` advisory present, current platform macOS (probed),
   `start..done` progress.
2. `reason` isolated (`--task "Reply with exactly: PONG"`) -- success,
   `effectiveModel` starts with `grok-4.5`, no `changedFiles`, `start..done`
   progress, private home destroyed clean.
3. `reason --schema` with the verify verdict schema -- structured extraction
   works live at `response.structured` (top-level `structuredOutput`).
4. `reason --web` -- success, `policy.webAccess` true, `web_search` in the tool
   allowlist, and the answer carries a live version token (D-WEB).
5. `review --target wrapper` -- success, `instructions[]`
   carries the repo-root `AGENTS.md`/`CLAUDE.md` pair, and `git status` is
   byte-identical before and after (zero repo writes).
6. Parallel isolation -- two concurrent `reason` runs both succeed with distinct
   run ids, session ids, and isolated working directories, neither cancelled.
   This is the live proof of the unique-leader-socket + private-home isolation.

Informational probes (recorded as evidence, never gate the result):

7. Raw-CLI `--check` deferral evidence -- ONE raw `grok ... --check` run in an
   isolated private home and temp cwd (never through the wrapper), recording
   whether a self-verification loop ran. v1 keeps `--check` unexposed regardless
   (C8); `verify` mode remains the independent-verification path.
8. Max-turns stop-token capture -- a raw `grok --max-turns 1` run with a read
   tool, to observe the real turn-exhaustion stop reason and pin
   `grokcli_output`'s matcher against it. If the run does not cleanly hit the
   budget, the token is recorded as unverified.

## Isolation and secrets discipline

Every wrapper-driven probe relies on the wrapper's own per-run private home
(auth material copied at 0600, destroyed on every path). The two raw-CLI probes
(7 and 8) build their OWN isolated private home the same way Task 0 did, and tear
it down auth-material-FIRST in a `finally`. No probe ever reads, prints, hashes,
or stores authentication file contents; the C4 envelope is secret-free by
construction (`envelope.assert_no_secret_material`). The only in-repo cwd a real
model ever runs against is the read-only `review` target, and that run is
asserted to leave `git status` untouched.

## Refreshing the last-validated stamp after a new Grok release

`accepted-version.json` is advisory evidence only (`enforcement: none`). When
Grok ships a new build, optionally run `--revalidate` after a green suite so the
stamp matches what you just probed. On red runs, leave the stamp alone and fix
wrapper assumptions (or escalate) - never fake-green the stamp. Runtime continues
to accept any working CLI either way.
