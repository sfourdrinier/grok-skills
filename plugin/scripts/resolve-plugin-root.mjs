#!/usr/bin/env node
// Thin CLI entry so skills can resolve the install root without env:
//   node "$SKILL_DIR/../../scripts/resolve-plugin-root.mjs" --skill-dir "$SKILL_DIR" --companion
import { main } from "./lib/resolve-plugin-root.mjs";
process.exit(main(process.argv.slice(2), process.env));
