#!/usr/bin/env node
// Self-locating agent entry. Plugin root = parent of agents/ (from this file's path).
// Usage (Claude plugin agents usually have CLAUDE_PLUGIN_ROOT set):
//   node "${CLAUDE_PLUGIN_ROOT}/agents/run.mjs" code --target '.' --base 'HEAD' --task-file - <<'GROK_TASK'
//   ...
//   GROK_TASK
// Codex managed agents inject an absolute path to this file as GROK_AGENT_RUN.
import { runFromPluginEntry } from "../scripts/lib/skill-run.mjs";

process.exitCode = runFromPluginEntry(import.meta.url, process.argv.slice(2));
