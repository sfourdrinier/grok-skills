// plugin/scripts/tests/command-injection.test.mjs
//
// Guardrail: no /grok:* skill markdown may interpolate raw slash-command
// arguments ($ARGUMENTS / positional $1.. / $@) into a `!`-bang line, because a
// bang line is executed by the shell at command-expansion time BEFORE the
// wrapper ever validates the run id -- so a pasted value containing $(...) or
// ;/&& would run locally in the operator's shell (double-quoting does NOT help;
// command substitution fires inside double quotes). The safe pattern is a
// model-driven Bash tool call that passes the argument as a literal argv element
// which grok_agent.py then validates against the strict run-id shape.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_DIR = path.resolve(SCRIPT_DIR, "..", "..");
const SKILLS_DIR = path.join(PLUGIN_DIR, "skills");
const AGENTS_DIR = path.join(PLUGIN_DIR, "agents");
const CODEX_AGENTS_DIR = path.join(PLUGIN_DIR, "codex-agents");

function agentAndTomlFiles() {
  const agents = fs.existsSync(AGENTS_DIR)
    ? fs
        .readdirSync(AGENTS_DIR)
        .filter((n) => n.endsWith(".md"))
        .map((n) => path.join(AGENTS_DIR, n))
    : [];
  const tomls = fs.existsSync(CODEX_AGENTS_DIR)
    ? fs
        .readdirSync(CODEX_AGENTS_DIR)
        .filter((n) => n.endsWith(".toml"))
        .map((n) => path.join(CODEX_AGENTS_DIR, n))
    : [];
  return [...agents, ...tomls];
}

function allInvocationDocs() {
  return [...skillFiles(), ...agentAndTomlFiles()];
}

// A companion INVOCATION line (the model-driven Bash call that runs the wrapper).
const COMPANION_INVOCATION = /grok-companion\.mjs/;
// A free-text task placed in a shell-evaluated position: `--task "<...>"`. The
// shell command-substitutes $(...)/backticks inside those double quotes before
// the wrapper ever sees the task, so the ONLY safe channel is `--task-file -`
// with a single-quoted heredoc. `--task-file` is deliberately excluded (the
// `-file` suffix means the negative lookahead below never fires on it).
const SHELL_EVALUATED_TASK = /--task(?!-file)\s+"/;

// A Claude Code bang line: after optional whitespace the content begins with `!`
// immediately followed by a backtick (the bang-executed shell command). This is
// the only markdown construct the harness hands straight to the shell.
const BANG_LINE = /^\s*!\s*`/;

// Any token the harness expands into a bang line from user-controlled input.
const ARG_INTERPOLATION = /\$ARGUMENTS\b|\$\{ARGUMENTS|\$@|\$[0-9]/;

// User-controlled, value-bearing wrapper flags. Beyond the free-text task and the
// status/cleanup run ids that earlier fixes hardened, these flags also carry
// operator-supplied values (paths, revisions, model ids). If a documented
// invocation substitutes a hostile value (containing $(...) or backticks) into
// one of these WITHOUT single-quoting it, the shell command-substitutes the value
// locally BEFORE the companion/wrapper ever validates it -- the same injection
// class as an unsafe `--task "..."`. The safe form single-quotes every
// substituted value so the shell passes the bytes verbatim as one literal argv
// token. `--task-file` is included, but its stdin sentinel value `-` is exempt
// (it carries no path to evaluate; free-text task safety has its own test above).
const USER_VALUE_FLAG =
  /--(?:target|base|worktree|run-id|input|rules-file|schema|model|task-file)\s+(\S+)/g;

// A line the harness hands to the shell: a model-driven companion invocation or a
// `!`-bang line. Only these positions can command-substitute a flag value.
function isShellInvocationLine(line) {
  return COMPANION_INVOCATION.test(line) || BANG_LINE.test(line);
}

function skillFiles() {
  return fs
    .readdirSync(SKILLS_DIR, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(SKILLS_DIR, entry.name, "SKILL.md"))
    .filter((file) => fs.existsSync(file));
}

test("no skill markdown interpolates raw arguments into a bang-executed line", () => {
  const offenders = [];
  for (const file of skillFiles()) {
    const lines = fs.readFileSync(file, "utf8").split("\n");
    lines.forEach((line, index) => {
      if (BANG_LINE.test(line) && ARG_INTERPOLATION.test(line)) {
        offenders.push(`${path.relative(SKILLS_DIR, file)}:${index + 1}: ${line.trim()}`);
      }
    });
  }
  assert.deepEqual(
    offenders,
    [],
    "bang lines must never embed raw slash-command arguments (shell-injection surface):\n" +
      offenders.join("\n")
  );
});

test("status and cleanup pass the run id through a model-driven Bash call, not a bang line", () => {
  for (const name of ["status", "cleanup"]) {
    const file = path.join(SKILLS_DIR, name, "SKILL.md");
    const contents = fs.readFileSync(file, "utf8");
    const hasBang = contents.split("\n").some((line) => BANG_LINE.test(line));
    assert.equal(hasBang, false, `${name} must not use a bang-executed line for a run-id argument`);
    assert.ok(
      contents.includes("grok-companion.mjs"),
      `${name} must still invoke the companion via a Bash call`
    );
  }
});

test("no companion invocation documents a shell-evaluated --task free-text argument", () => {
  // PR968 codex rescue-task-injection: every documented companion call that
  // carries free-text task content must route it through `--task-file -` + a
  // single-quoted heredoc (stdin), never a `--task "<text>"` the shell would
  // command-substitute. Scans the commands AND the rescue agent (which builds the
  // call from an untrusted natural-language request).
  const files = allInvocationDocs();
  const offenders = [];
  for (const file of files) {
    const lines = fs.readFileSync(file, "utf8").split("\n");
    lines.forEach((line, index) => {
      if (COMPANION_INVOCATION.test(line) && SHELL_EVALUATED_TASK.test(line)) {
        offenders.push(`${path.relative(PLUGIN_DIR, file)}:${index + 1}: ${line.trim()}`);
      }
    });
  }
  assert.deepEqual(
    offenders,
    [],
    "companion invocations must pass free-text task via --task-file - (stdin heredoc), never a shell-evaluated --task \"...\":\n" +
      offenders.join("\n")
  );
});

test("companion/bang invocation lines single-quote every user-controlled flag value", () => {
  // PR968 codex argv-safe user-controlled command flags: extends the free-text
  // `--task` hardening to ALL operator-supplied flag values (--target, --base,
  // --worktree, --run-id, --input, --rules-file, --schema, --model, --task-file
  // path). Every such value in a shell-evaluable position (companion invocation
  // or bang line) MUST be single-quoted so a value containing $(...) or backticks
  // reaches the wrapper literally instead of running in the operator's shell.
  const files = allInvocationDocs();
  const offenders = [];
  for (const file of files) {
    const lines = fs.readFileSync(file, "utf8").split("\n");
    lines.forEach((line, index) => {
      if (!isShellInvocationLine(line)) return;
      for (const match of line.matchAll(USER_VALUE_FLAG)) {
        const value = match[1];
        // The `--task-file -` stdin sentinel carries no shell-evaluable content.
        if (value === "-") continue;
        // Safe: a single-quoted value cannot be command-substituted by the shell.
        if (value.startsWith("'")) continue;
        // Template placeholders in docs (e.g. $GROK_COMPANION) are not user values.
        if (value.startsWith("$") || value.startsWith('"') || value === "\\") continue;
        offenders.push(`${path.relative(PLUGIN_DIR, file)}:${index + 1}: ${match[0]}`);
      }
    });
  }
  assert.deepEqual(
    offenders,
    [],
    "user-controlled flag values on a companion/bang invocation line must be single-quoted so the shell cannot command-substitute them:\n" +
      offenders.join("\n")
  );
});

test("every skill markdown is present to scan", () => {
  // Sanity: the guardrail is only meaningful if it actually scanned files.
  assert.ok(skillFiles().length >= 1, "expected at least one skill SKILL.md to scan");
  assert.ok(agentAndTomlFiles().length >= 2, "expected Claude agents and Codex TOML templates");
});

test("Claude agents restrict tools to Bash(node:*)", () => {
  for (const file of fs.readdirSync(AGENTS_DIR).filter((n) => n.endsWith(".md"))) {
    const body = fs.readFileSync(path.join(AGENTS_DIR, file), "utf8");
    assert.match(
      body,
      /^tools:\s*Bash\(node:\*\)\s*$/m,
      `${file} must set tools: Bash(node:*) (not unrestricted Bash)`
    );
    assert.ok(
      /never invent (versioned )?cache paths/i.test(body),
      `${file} must forbid inventing cache paths`
    );
  }
});

test("grok-rescue description does not steal pure implementation work", () => {
  const body = fs.readFileSync(path.join(AGENTS_DIR, "grok-rescue.md"), "utf8");
  const fm = body.split("---")[1] || "";
  assert.ok(
    /engineer-coder/i.test(body),
    "rescue must point implementation work at grok-engineer-coder"
  );
  assert.ok(
    !/substantial coding task/i.test(fm),
    "rescue frontmatter should not claim substantial coding as primary"
  );
  assert.ok(
    /not\s+for pure implementation|prefer for investigation/i.test(fm),
    "rescue description should deprioritize pure implementation"
  );
});
