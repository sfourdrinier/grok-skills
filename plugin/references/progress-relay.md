<!-- plugin/references/progress-relay.md -->

# T2-2 progress relay: chosen surfacing mechanism (grounding, T2-2.0)

**Historical design snapshot (T2-2.0).** Production `grok-companion.mjs` is a
multi-command harness (jobs, LiveRelay, direct mode, setup, entry-root force,
etc.), not only a pure forwarder. The relay mechanism below still applies for
live progress; read `plugin/scripts/grok-companion.mjs` for current behavior.

The relay surfaces a Grok run's live progress (rich thought/tool activity from
T2-0) as human-visible lines, without touching any safety boundary and without
changing the hardened Python wrapper (`grok_agent.py`).

## The question (T2-2.0)

When a slash command runs `node grok-companion.mjs <mode>` through the Bash
tool, how does live progress reach the user? Two candidate mechanisms:

- (a) a pure plugin-side relay: the shim tail-follows the run's `progress.jsonl`
  and prints human-readable progress to STDERR as events arrive, while STDOUT
  stays the single wrapper-authored envelope (the Bash tool surfaces stderr).
- (b) an additive wrapper flag (`--stream-progress`, stderr-only) if reading the
  file externally cannot follow live.

## Evidence gathered

1. The shim (`grok-companion.mjs`) is a PURE forwarder that today runs the
   wrapper with `spawnSync(..., { stdio: "inherit" })`. stdout is the wrapper's
   single C4 envelope, written once at the very end (`envelope.emit_envelope` is
   the only stdout writer in the whole groklib package).
2. T2-0 writes each streamed thought/text token event into the run's
   `progress.jsonl` via `ProgressWriter.emit` (coalesced, phase `"grok"`,
   `data = {event, chars, text}`). Confirmed against a real run:
   `{"data":{"chars":71,"event":"thought","text":"The user wants me to reply ..."},"phase":"grok","seq":5,...}`.
3. The wrapper does NOT echo those coalesced progress events to its own stderr
   (`grokcli._relay_stream` calls `progress.emit` only; stderr carries the child
   process's own stderr plus diagnostic `log_stderr` lines, not the progress
   feed). So letting Bash pass stdout/stderr through UNCHANGED does not surface
   live progress: the events live only in the file.
4. The progress path is deterministic once a run exists:
   `state_root()/runs/<run_id>/progress.jsonl`, where `state_root()` is
   `$XDG_STATE_HOME/grok-skills` (else `~/.local/state/grok-skills`),
   per `runstate.state_root` / `runstate._run_paths_for`. The C4 envelope and the
   run record both expose it as `progressStreamPath`.
5. The run id is minted INSIDE the wrapper (`runstate.new_run_id`) and is not
   known to the shim up front. But the run DIRECTORY appears under `runs/` as
   soon as `runstate.create_run` runs (early in every mode), so the shim can
   discover the freshly created run dir by snapshotting `runs/` before launch and
   picking the new entry that appears after start.

## Decision: mechanism (a), a pure plugin-side relay. No wrapper change.

Reading the file externally CAN follow live (evidence 4 + 5), so (b) is not
needed and is NOT added. The wrapper stays byte-for-byte unchanged; no safety
control is touched. The relay writes ONLY to stderr; stdout remains the
wrapper's envelope alone.

### Foreground (review / reason / code)

The shim launches the wrapper with `spawn(..., { stdio: "inherit" })` (stdout
still inherited, so the wrapper writes its envelope straight to the real stdout,
verbatim and exactly once, with the shim never touching it). Concurrently a
`LiveRelay`:

1. snapshots existing run-id dirs before launch,
2. polls `runs/` until a new run dir (valid id, created at/after start) appears,
3. tail-follows that dir's `progress.jsonl`, formatting each new event to stderr
   in order (index-based dedup, tolerant of the torn trailing line the append
   race produces), and
4. does a final drain when the child exits, then stops.

### Background (status)

`/grok:status --run-id <id>` already returns the wrapper's status envelope
(which embeds the events). The shim additionally renders that run's
`progress.jsonl` (deterministic path from `--run-id`) to stderr before running
the wrapper passthrough, so the human sees the progress feed while stdout stays
the verbatim envelope.

## Degrade-to-Tier-1 state machine (Codex review item 3 - three timings)

The relay is strictly best-effort and can never change, lose, or duplicate the
wrapper's result; every relay operation is wrapped so a failure is logged to
stderr and the relay simply stops.

- (i) relay/stream unavailable at start (state root missing/unreadable, run dir
  never discovered): nothing is emitted; the wrapper still runs and its envelope
  still reaches stdout. Plain Tier-1 behavior.
- (ii) relay fails mid-run (progress file unreadable, a follower tick throws):
  the tick error is caught and the relay disables itself; the wrapper subprocess
  and its exit code are untouched, so the envelope is still delivered exactly
  once.
- (iii) the run itself fails: the wrapper writes its failure envelope to stdout
  and exits non-zero; the shim passes that exit code through unchanged and the
  relay just stops. The failure envelope is delivered verbatim.

stdout is inherited from the wrapper in every path, so the envelope is authored
once by the wrapper and the relay/shim never write stdout - exactly-once and
verbatim are structural, not merely conventional.
