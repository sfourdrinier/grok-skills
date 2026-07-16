<!-- plugin/wrapper/references/authority-policies.md -->

# Authority policies

Per-mode capability tables (spec section 5) and the C4 error-class registry
with its operational meaning. This is reference material, not a tutorial -
see `../SKILL.md` for when to reach for each mode and `cli-reference.md` for
the underlying probe evidence.

## Per-mode capability table

| Mode | Working directory | Tools (`--tools`) | Sandbox profile | Network | Subagents | Web (`--web`) | Permission mode |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `review` | Repo workspace from `--target` (existing dir inside a git repo; root = git toplevel of that target, not the wrapper install). Default: **live checkout**. Opt-in `--isolated`: owned external worktree under state root with tracked dirty applied (fail closed `isolation-unavailable`). `--base` is comparison framing only and does **not** force isolation. | `read_file`, `grep`, `list_dir` (read-only; FS drift / change-shaped JSON keys → informational warnings, not hard fail) | `read-only` (built-in; write denial only) | Permitted (D-NET) | Disabled (`--no-subagents` always) | Opt-in per run; adds `web_search`, `web_fetch`, `open_page`, `open_page_with_find` and omits `--disable-web-search` | `auto` |
| `reason` | Fresh private temp dir OUTSIDE the repo; no repository rule discovery | `read_file` only when at least one `--input` artifact is supplied, else no tools | `read-only` (built-in; write denial only) | Permitted (D-NET) | Disabled | Opt-in per run (same as `review`) | `auto` |
| `code` | External git worktree the wrapper creates and verifies (never the current checkout) | Editing and terminal tools (`write`, `search_replace`, `run_terminal_command`, `get_command_or_subagent_output`) plus read tools (`read_file`, `list_dir`, `grep`), confined by the sandbox to the worktree | `workspace` (built-in; confines writes to cwd plus private temp) | Permitted (D-NET) | Disabled | Opt-in per run (same as `review`) | `auto` |
| `verify` | An EXISTING external worktree named by `--worktree`; source-editing tools absent | Read-only set plus terminal tools for approved verification commands only; no `write`/`search_replace` | `workspace` (same base profile and confinement as `code`) | Permitted (D-NET) | Disabled | **Never** (hermetic by design; `verify` has no `--web` flag) | `auto` |
| `preflight` | N/A (no task-bearing Grok run; only short `grok models` / `grok inspect --json` probes in a throwaway private home) | N/A | N/A | N/A | N/A | N/A | N/A |
| `status` | N/A (pure local read of stored run state; never invokes Grok; **never writes** the target run dir). Top-level envelope: `running` (exit 0) for in-flight lifecycle; `success` (exit 0) for completed; `failure` (exit 1) for failed/canceled/derived interrupted — still a well-formed status envelope. Effective lifecycle: record → valid envelope → derived `interrupted`. | N/A | N/A | N/A | N/A | N/A | N/A |
| `cleanup` | N/A (pure local read/remove of stored run state and, for code/verify, the worktree; never invokes Grok) | N/A | N/A | N/A | N/A | N/A | N/A |

Notes:

- **Memory** is disabled (`--no-memory`) in every Grok-spawning mode, no
  exceptions and no flag to re-enable it.
- **Permission mode** is `auto` in every mode (`HEADLESS_PERMISSION_MODE`,
  pinned by live probe evidence in `cli-reference.md`). Under `auto`, the
  security boundary is the `--tools` allowlist plus the sandbox profile, not
  an interactive approval prompt: a tool outside the allowlist is hard-denied
  (`stopReason Cancelled`, mapped to error class `cancelled`), never queued
  and never silently allowed.
- **Sandbox profiles enforce WRITE confinement only.** Neither built-in
  profile (`read-only`, `workspace`) nor any custom `sandbox.toml` profile
  the wrapper has been able to construct denies reads of credential or
  external-secret paths on the last-probed Grok CLI (decision D-SECRETREAD).
  Every mode above therefore carries the same accepted read-gap residual
  described in `../SKILL.md` and `cli-reference.md`; the sandbox column above
  names the WRITE boundary that IS enforced and checked via
  `sandbox-events.jsonl` telemetry after every run.
- **Network egress is permitted for every mode** (decision D-NET); the
  original spec requirement to deny it was withdrawn because no built-in or
  custom sandbox profile could enforce it for terminal-tool subprocesses.
- **`--web` never changes network policy.** It only changes which Grok
  built-in tools are allowlisted; child-process network access identified
  above is governed solely by D-NET and is the same with or without `--web`.
- Live execution of `review`, `reason`, `code`, and `verify` additionally
  requires the current platform to be in `PROBED_PLATFORMS` (macOS only in
  version 1, decision D-PORT); on any other platform every one of these
  modes fails closed with error class `probe-required` before Grok is ever
  spawned.

## C4 error classes and what they mean for you

Every failure envelope's `error.class` is exactly one of the 27 registered
values below (`groklib/envelope.py: ERROR_CLASSES`). `error.message` and
`error.detail` carry the specifics; this table is the quick index of what
each class implies and what to do next.

| Error class | What it means | What to do |
| --- | --- | --- |
| `auth-missing` | `~/.grok/auth.json` is absent, or the private-home login probe (`grok models`) reports not logged in. | Log in with the real `grok` CLI, confirm `~/.grok/auth.json` exists, then retry. |
| `version-mismatch` | `grok --version` could not run, exited nonzero, or printed no usable Grok version line. (Exact build mismatch is **not** an error.) | Install/fix the Grok CLI so `grok --version` works. |
| `model-unavailable` | The requested model (default `grok-4.5`) is not in the selectable model list from `grok models`, or the run's effective model was not in the requested model family. | Check account/model access; do not silently switch models. |
| `invalid-target` | `--target`, `--worktree`, `--input`, `--rules-file`, or `--task-file` does not exist, is outside the relevant repo, or is otherwise malformed. | Fix the path; relative paths resolve against the **process cwd**, and the git repo root is derived from the resolved path (not from where `grok_agent.py` is installed). |
| `rules-parity-failure` | With `ruleFileParity` enabled, the `AGENTS.md`/`CLAUDE.md` pair at some directory level is missing one file, fails the path-header convention, or the bodies differ after line 1. | Fix the pair so both files exist with matching bodies (and valid headers), or disable `ruleFileParity` in `.grok-skills.json`; the wrapper will not guess which file is authoritative. |
| `worktree-failure` | The wrapper could not create, verify, or remove the external git worktree (collision, missing metadata, dirty worktree on cleanup, etc). | Inspect `worktreePath`/`worktreeBranch` in the envelope; a dirty worktree on cleanup is preserved, never force-removed. |
| `sandbox-failure` | Sandbox enforcement telemetry (`sandbox-events.jsonl`) is absent, the applied profile mismatches the requested one, `enforced` was not `true`, or a write escaped the run's legitimate writable roots. | Treat as a hard stop; do not retry without investigating - this is the wrapper's core write-confinement guarantee failing to verify. |
| `wrong-working-directory` | The `code`-mode sentinel file (`.grok-run-<run-id>`) was not found inside the verified worktree, or was found inside the original checkout. | Treat the run as compromised; inspect via `status` before trusting any reported changes. |
| `tool-unavailable` | A required tool is missing - most commonly the `grok` binary itself. | Confirm the binary path (`GROK_AGENT_BINARY` override or the default `~/.grok/bin/grok`). |
| `verifier-unavailable` | `verify` mode could not extract a valid verdict (`pass`/`fail`/`inconclusive` plus evidence) from the schema-constrained structured output. | Re-run with a clearer verification task; do not infer a verdict from prose. |
| `output-missing` | Grok produced empty stdout. | Check `error.detail` for the exit status and captured stderr; retry once, then investigate if it recurs. |
| `output-malformed` | Grok's stdout was not parseable JSON, or (for `status`) a stored envelope failed C4 structural validation on read-back. | Never trust the raw output in this state; `status` explicitly refuses to re-emit a malformed stored document verbatim. |
| `schema-mismatch` | Structured output failed the caller's `--schema` (review/reason) or `verify`'s fixed verdict schema; `error.detail.pointer` names the failing location. | Adjust the schema or the task so Grok's structured answer can satisfy it. |
| `timeout` | The run exceeded its wall-clock `--timeout`; the whole process tree was killed. | Raise `--timeout` if the task is legitimately long, or narrow the task. |
| `turn-exhaustion` | Operator set `--max-turns` and Grok hit that budget with no usable text/structured output. (Default runs omit `--max-turns` entirely.) | Only applies when you set an explicit budget; omit the flag for unlimited. If you set a budget, raise it or narrow the task. |
| `cancelled` | Grok's `stopReason` was `Cancelled` with **no** salvageable findings. If Cancelled arrives **with** text/structured output, the wrapper keeps findings as success + warning (does not discard them). | Empty cancel: often a hard-denied tool under `permission-mode auto`. Cancelled-with-content is treated as incomplete success, not a wipe. |
| `cli-failure` | Grok exited nonzero for a reason not covered by a more specific class, or an unexpected wrapper exception occurred. | `error.detail` carries captured stderr; report it if it recurs across preflight-clean runs. |
| `unexpected-edits` | A `code`/`verify` run wrote outside the allowed worktree or modified the original checkout (escape). **Not** used for read-only `review` when the tree moves or Grok lists change-shaped JSON keys — those are informational warnings only. | Investigate worktree escapes before re-running `code`/`verify`. Review warnings about changed paths: keep the findings. |
| `isolation-unavailable` | Opt-in `review --isolated` could not create or prepare the owned worktree (path/branch collision, dirty submodule, patch apply failure, etc.). No silent fallback to the live checkout. | Fix the setup cause, or re-run without `--isolated` for a live-tree review. |
| `validation-failure` | A required build-gate command (build, typecheck, lint, or test) failed in `code` mode, or a `preflight` structural check (e.g. state root permissions) failed. | Check `commands[]` in the envelope for the failing command and its captured exit status. |
| `cleanup-failure` | The private Grok home or the run's worktree could not be destroyed/removed cleanly. | Never treat this as harmless - it can mean authentication material was not confirmed removed; investigate immediately. |
| `state-ownership-violation` | An `owner.json` marker did not match the expected run id/owner string (for example, `status`/`cleanup` pointed at state the wrapper does not own). | Do not force past this; it exists to stop one run from touching another run's or another tool's state. |
| `leader-socket-failure` | The generated `--leader-socket` path exceeded the OS `AF_UNIX` path length limit (~104 bytes on macOS). | This should not occur with the current short prefix scheme; report it if it does. |
| `usage-error` | Bad argv - missing/mutually-exclusive/malformed CLI flags - caught before any run was created. | Fix the command line; compare against the exact C8 surface in `cli-reference.md`, never guess a flag. |
| `probe-required` | The current platform (or a specific enforcement claim) has no captured live-probe evidence backing it. | On version 1 this means: you are not on macOS. Live modes stay blocked until that platform's own probe suite runs and is committed. |
| `finalization-timeout` | The finalize worker exceeded its wall-clock budget; parent recovered after kill and durably wrote a terminal failure (or fail-closed if persist itself failed). | Inspect `error.detail.budgetSeconds` and `finalize-worker.stderr` under the run dir; retry only after checking disk/CAS pressure. |
| `finalization-worker-missing-result` | The finalize worker exited 0 (or parent recovery finished) without a valid terminal `envelope.json`. | Treat as durable hang recovery failure; inspect the run dir and re-run if the task outcome was not persisted. |
| `finalization-worker-unkillable` | Parent could not terminate the finalize worker after timeout; **no durable terminal write** was performed (`doNotStore`). Lifecycle may remain `finalizing`. | Investigate the stuck worker process; do not trust stdout-only ephemeral failure as stored state — poll `status` until lifecycle moves or intervene manually. |
