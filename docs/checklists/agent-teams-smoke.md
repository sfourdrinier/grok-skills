<!-- docs/checklists/agent-teams-smoke.md -->

# Agent-teams smoke (Claude Code experimental, Phase 3 checklist)

Goal: confirm grok-engineer-coder works as a TEAMMATE (long-lived,
SendMessage-addressable) under CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1.
Run manually in an interactive Claude Code session; append dated result rows.

1. export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1; start a session in a
   scratch git repo with the grok plugin enabled.
2. Spawn a teammate from grok:grok-engineer-coder; delegate one tiny
   implement cycle (contract -> code -> handoff READY).
3. SendMessage a follow-up ("continue that run: <small change>"); confirm the
   teammate uses code --continue-run and re-handoffs.
4. Confirm the handoff protocol held (no auto-apply; envelopes relayed).

| date | claude-code version | result | notes |
|------|---------------------|--------|-------|
| (pending first run) | | | |
