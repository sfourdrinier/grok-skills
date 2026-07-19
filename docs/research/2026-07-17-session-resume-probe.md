<!-- docs/research/2026-07-17-session-resume-probe.md -->

# Session resume probe (Phase 2 gate) - GO

Date: 2026-07-17. CLI: grok 0.2.102 (ab5ebf69acec) [stable]. macOS.
Method: two headless runs in throwaway private HOMEs (0700, auth.json copied
0600, dir scrubbed after the probe), mirroring what groklib/session_store.py
will do.

## Procedure and observations

1. HOME1 + `grok -p 'Remember the word pineapple...' --session-id <uuid4>
   --output-format plain --disable-web-search --no-subagents` -> "ok".
2. Store layout under HOME1:
   `~/.grok/sessions/<URL-encoded-cwd>/<session-uuid>` plus
   `prompt_history.jsonl` per cwd bucket and `session_search.sqlite` at the
   sessions root. Sessions are BUCKETED BY CWD.
3. Fresh HOME2 with auth.json + the copied `sessions/` dir, same cwd:
   - `--session-id <same uuid>` FAILS CLOSED: "Session ID ... is already in
     use" (create-only when the id exists in the store; cleanly
     distinguishable error).
   - `--resume <same uuid>` SUCCEEDS: answered "pineapple".

## Design consequences (Tasks 2.1 / 2.2)

- GO for the session-archive approach; fallback Task 2.4 not needed.
- Archive/seed the WHOLE `<home>/.grok/sessions` dir (bucket encoding is the
  CLI's concern, not ours). It contains prompt history (task text): 0700/0600
  permissions, run-dir confinement, cleanup removes it with the run dir.
- Continuation runs must execute from the SAME worktree path (cwd bucket);
  retained worktrees satisfy this naturally.
- build_argv: continuation passes `--resume <id>` INSTEAD of
  `--session-id <id>` (new-run path keeps `--session-id <uuid4>`); the C6
  baseline flag set gains `--resume` for continuation runs only, and the
  wrong-flag failure mode is a distinguishable CLI error.
- Not probed here: composition with `--prompt-file` + `--output-format
  streaming-json` (the wrapper's invocation shape); covered by Task 2.2's
  live smoke before integration.
