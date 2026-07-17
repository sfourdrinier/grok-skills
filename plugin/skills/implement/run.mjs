#!/usr/bin/env node
// Self-locating skill entry. Plugin root = parent of skills/ (from this file's path).
// Usage: node "$SKILL_BASE/run.mjs" <companion-mode> [args...]
// SKILL_BASE = Skill tool "Base directory for this skill" (absolute).
import { runFromSkillEntry } from "../../scripts/lib/skill-run.mjs";

process.exitCode = runFromSkillEntry(import.meta.url, process.argv.slice(2));
