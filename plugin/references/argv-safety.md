<!-- plugin/references/argv-safety.md -->

# Argv and task-text injection safety (canonical)

## Task text

The task is free text you must NEVER place in a shell-evaluated position.
`$(...)`/backticks inside a double-quoted `--task "..."` run locally BEFORE the
wrapper validates them. When the arguments carry a `--task <text>`, deliver that
text on STDIN with `--task-file -` and a SINGLE-QUOTED heredoc so the shell
passes it byte-for-byte; the companion stages it into a temp file for the
wrapper.

## Flag values

Wrap every substituted flag VALUE (`--target`, `--base`, a `--task-file <path>`,
`--model`, `--timeout`, `--max-turns`, `--run-id`, `--worktree`, each `--input`,
each `--rules-file`, and EVERY other value you substitute from `$ARGUMENTS`) in
SINGLE quotes, for example `--target '<path>' --base '<revision>'`. Single
quotes stop the shell from evaluating `$(...)`/backticks, so a hostile value
reaches the companion as one literal argv token and the wrapper validates it
(target/worktree path resolution + escape guards). An unquoted OR double-quoted
value would be command-substituted locally BEFORE the wrapper ever sees it --
the same injection class as an unsafe `--task "..."`. Bare flags (`--web`,
`--confirm`) carry no value to quote.

## Last-valid flag values (companion SSOT)

Companion parsing of value-bearing flags uses **last valid** (split or equals
form), owned by `plugin/scripts/lib/companion-args.mjs` (`flagValue`,
`flagOccurrences`, `stripValueFlag`). A following flag is never consumed as a
value. A later bare duplicate without a value does **not** wipe a prior good
value. Direct-mode and task-file staging stay argparse-parity with the wrapper:

- `--task` / `--task-file` (including `--task=` / `--task-file=`): last **valid**
  value wins; task-file-over-task policy and stdin sentinel `--task-file -` /
  `--task-file=-` staging are preserved
- Other value flags (`--run-id`, `--integration`, `--target`, `--base`,
  `--timeout`, ...) use the same last-valid SSOT
- `--web` / `--no-web` (including equals forms): last occurrence wins via
  `resolveWebFlag`; prefix-safe (`--web-search` is not `--web`)
- Hermetic modes that force web off, schema refusal, and D-WEB tool expansion
  still apply after resolution

## Contract requiredValidation argv is shell-free

The wrapper runs contract `requiredValidation` entries with `shell=False`: each
argv array is executed verbatim, with NO shell expansion. No globs
(`tests/*.test.mjs`), no directory shorthands that rely on shell or runner
defaults, no `$VARS`, no pipes or redirection. Use explicit argv forms, for
example `["python3", "-m", "unittest", "discover", "-s", "tests", "-q"]` or
`["node", "--test"]` with cwd set to the directory whose default test glob you
want. (Observed live 2026-07-17: `["node", "--test", "tests/"]` failed inside
the wrapper while the same line worked in an interactive shell.)
