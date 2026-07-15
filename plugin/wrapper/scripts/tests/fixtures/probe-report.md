<!-- plugin/wrapper/scripts/tests/fixtures/probe-report.md -->

# Task 0 Grounding Probe Report

Live, read-only probes of the installed Grok CLI, run on 2026-07-14 (UTC).
Every Grok invocation used an isolated private HOME (temp dir with only `auth.json`
copied in at mode 0600) and a unique `--leader-socket` path. No probe wrote into
this repository except the four Task 0 deliverable files. No authentication file
contents were read, printed, hashed, or stored; only file names and permission
bits were recorded, plus the stdout/stderr of `grok` invocations.

Machine: darwin 25.5.0, arm64 (aarch64). Grok binary is a symlink
`~/.grok/bin/grok -> ../downloads/grok-0.2.101-macos-aarch64`.

Legend: `H` is an isolated private home from `mktemp -d -t grok-skills-home-`;
`H/.grok/auth.json` is a 0600 copy of `~/.grok/auth.json`. `T` and `R` are fresh
temp working directories under the OS temp dir (`R` is a disposable `git init`
repo). Timeout on each model-calling probe was 600000 ms; none timed out.

---

## Pinned constants (authoritative)

| Constant | Pinned value | Evidence |
| --- | --- | --- |
| Accepted version (first line of `grok --version`) | `grok 0.2.101 (5bc4b5dfadcf) [stable]` | Step 1 |
| Accepted semver | `0.2.101` | Step 1, `grok inspect` `grokVersion` |
| `HEADLESS_PERMISSION_MODE` | `auto` | Steps 4, 5 |
| `SANDBOX_PROFILE_BY_MODE.review` | `read-only` (write denial only; secret-read and network denial are `probe-required`) | Steps 3, 5c, 5d |
| `SANDBOX_PROFILE_BY_MODE.reason` | `read-only` (write denial only; secret-read and network denial are `probe-required`) | Steps 3, 5c, 5d |
| `SANDBOX_PROFILE_BY_MODE.code` | `workspace` (write confinement to cwd plus private temp only; secret-read and network denial are `probe-required`) | Steps 5a, 5b, 5c, 5d |
| `SANDBOX_PROFILE_BY_MODE.verify` | `workspace` (same confinement as code; no source-editing tools via `--tools`) | Steps 5a, 5b, 5c, 5d |
| Auth file under `~/.grok/` | `auth.json` (mode 0600). Companion `auth.json.lock` present but not auth material. | Step 2 |
| Leader isolation mechanism | Unique `--leader-socket <path>` per run plus a distinct private `HOME` per run. `--no-leader` exists only on `grok agent` subcommands, not plain `grok`. | Steps 2, 6 |

Default model `grok-4.5` is selectable and is the CLI default (Step 2).

### Safety gate: modes are `probe-required` for two spec requirements

Built-in sandbox profiles `read-only`, `workspace`, and `strict` all confine or
deny writes as expected, but NONE of them denies (a) reads of credential or
external secret locations, or (b) child-process network access. Spec sections
5.3.7, 8.1, and 8.2 require both denials. Because no built-in profile satisfies
them, every mode (review, reason, code, verify) must BLOCK as `probe-required`
for the secret-read-denial and network-denial requirements until a custom
sandbox profile (defined in `~/.grok/sandbox.toml` or `.grok/sandbox.toml`) that
adds those denials is authored and re-probed. Task 6 `policy_for_mode` already
raises `GrokWrapperError("probe-required")` for exactly this condition. Write
confinement itself is satisfied by the built-in profiles above. Sandbox
enforcement telemetry (the C4 `sandbox.evidence` source) is written to
`<home>/.grok/sandbox-events.jsonl`, not the stdout envelope (Step 5e); its
`restrict_network: true` flag is NOT trustworthy on its own because terminal
subprocesses bypassed it.

---

## Step 1: Binary identity

argv:
- `["<operator-home>/.grok/bin/grok", "--version"]`

stdout (exit 0):
```
grok 0.2.101 (5bc4b5dfadcf) [stable]
```

`ls -la ~/.grok/bin/grok` showed the symlink to
`../downloads/grok-0.2.101-macos-aarch64`. The commit hash `5bc4b5dfadcf` is
embedded in that pinned downloaded artifact and is therefore stable for this
released version. The candidate pin becomes `grok 0.2.101 (5bc4b5dfadcf)
[stable]`; see the accepted-version.json note below.

## Step 2: Config surface inventory

`ls -la ~/.grok/` (names and permission bits only, contents never read).
Auth-bearing file identified: `auth.json` at mode `-rw-------` (0600). A
`trusted_folders.toml` (0600) and `auth.json.lock` (0644, empty) also exist;
`auth.json` is the sole authentication material per spec section 7 ("copies the
existing Grok authentication file"). Only `auth.json` was copied into `H/.grok/`
at 0600.

Isolated-home inspect:
- `H=$(mktemp -d -t grok-skills-home-)`; `mkdir -p H/.grok`; `chmod 700 H H/.grok`;
  `cp ~/.grok/auth.json H/.grok/auth.json`; `chmod 600 H/.grok/auth.json`.
- argv: `["<operator-home>/.grok/bin/grok", "inspect", "--json", "--leader-socket", "H/.grok/leader.sock"]` with `HOME=H`, cwd a fresh empty temp dir.
- exit 0, 3902 bytes stdout, empty stderr. Pretty-printed JSON saved to
  `inspect-shape.json` (no token-like or account values present).

Observations from `grok inspect --json`:
- Fields: `grokVersion` (`0.2.101`), `channel`, `cwd`, `projectRoot`,
  `projectTrusted`, `projectInstructions`, `permissions`, `loginPolicy`,
  `hooks`, `skills`, `agents`, `plugins`, `marketplaces`, `mcpServers`,
  `lspServers`, `configSources`, `externalCompat`.
- `grok inspect --json` does NOT report a login boolean, the selectable model
  list, or the built-in tool inventory. It reports built-in subagent names
  (`general-purpose`, `explore`, `plan`) under `agents`, and no permission modes
  or sandbox profiles. See the deviation note for Task 7.

Login state, model list, and default model come instead from `grok models`:
- argv: `["<operator-home>/.grok/bin/grok", "models", "--leader-socket", "H/.grok/ls-m.sock"]`, `HOME=H`, exit 0.
- stdout:
```
You are logged in with grok.com.

Default model: grok-4.5

Available models:
  * grok-4.5 (default)
  - grok-composer-2.5-fast
```
- Conclusion: the copied `auth.json` alone makes the isolated home report as
  logged in. `grok-4.5` is the default and is selectable.

Permission modes and flag surface come from `grok --help` (exit 0). All C6
baseline flags exist: `--prompt-file`, `--verbatim`, `--cwd`, `--model`,
`--output-format {plain,json,streaming-json}`, `--json-schema`,
`--permission-mode {default,acceptEdits,auto,dontAsk,bypassPermissions,plan}`,
`--tools`, `--disallowed-tools`, `--allow`, `--deny`, `--no-subagents`,
`--no-memory`, `--disable-web-search`, `--no-plan`, `--sandbox`, `--max-turns`,
`--session-id`, `--leader-socket`. `grok agent --help` confirms `--no-leader`
exists only under `grok agent` subcommands.

Sandbox profile discovery. `grok --help` does NOT enumerate `--sandbox` values.
An intentionally invalid value returned (exit 1, before any model call):
- argv: `["grok", "-p", "hi", "--sandbox", "not-a-profile", "--leader-socket", "H/.grok/ls-a.sock"]`
```
warning: sandbox could not be applied: Custom sandbox profile 'not-a-profile' not found. Define it in ~/.grok/sandbox.toml or .grok/sandbox.toml:

[profiles.not-a-profile]
extends = "workspace"
read_only = ["/data"]

error: could not apply the 'not-a-profile' sandbox profile; refusing to start rather than run unsandboxed.
```
This proves: built-in base profiles exist (the example extends `workspace`);
custom profiles are defined in `sandbox.toml`; an unknown profile fails closed.
Confirmed working built-in profiles by direct use: `read-only`, `workspace`,
`strict` (Steps 3, 5). Invalid tool names passed to `--tools` and
`--disallowed-tools` are silently ignored (no enumeration via error text).

## Step 3: Headless JSON output shape (full C6 baseline)

argv (exit 0, no flag rejected):
```
["<operator-home>/.grok/bin/grok",
 "--prompt-file", "T/prompt.txt", "--verbatim", "--cwd", "T",
 "--output-format", "json", "--model", "grok-4.5",
 "--permission-mode", "dontAsk", "--no-subagents", "--no-memory",
 "--disable-web-search", "--no-plan", "--sandbox", "read-only",
 "--max-turns", "3", "--session-id", "<uuid4>",
 "--leader-socket", "H/.grok/leader-2.sock"]
```
prompt file content: `Reply with exactly: PONG`.

Raw stdout saved to `real-output-shape.json`. Shape (top-level keys):
- `text` (string): the final assistant text. Value `"PONG"`. This is where the
  final text lives.
- `stopReason` (string): stop reason field. Value `"EndTurn"` on success.
- `sessionId` (string): the wrapper-supplied session UUID.
- `requestId` (string): server-issued request UUID (not account-identifying).
- `thought` (string): model reasoning text.
- `usage` (object): `input_tokens`, `cache_read_input_tokens`, `output_tokens`,
  `reasoning_tokens`, `total_tokens` (snake_case).
- `num_turns` (int): agent turn count.
- `modelUsage` (object): keyed by the effective model id, each value has
  `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `modelCalls`
  (camelCase). With no tools the key is `grok-4.5`; once any tool runs the key
  is `grok-4.5-build` (see Step 4). Task 12 effective-model extraction must
  accept both.

Envelope mapping (C4): `grok.stopReason` <- `stopReason`; `grok.sessionId` <-
`sessionId`; `grok.requestId` <- `requestId`; `grok.modelUsage` <- `modelUsage`;
`usage.turns` <- `num_turns`; `usage.raw` <- `usage`; final text / `response`
<- `text`.

Effective-working-directory signal: NONE. The JSON output carries no cwd or
working-directory field. Task 11's `wrong-working-directory` check therefore
cannot read a cwd from the output and must rely on the sentinel-file mechanism
already specified in the plan (a `.grok-run-<run-id>` sentinel created inside the
worktree).

`--json-schema` variant (exit 0):
```
["grok", "--prompt-file", "T/prompt.txt", "--verbatim", "--cwd", "T",
 "--json-schema", "{\"type\":\"object\",\"required\":[\"answer\"],\"properties\":{\"answer\":{\"type\":\"string\"}}}",
 "--model", "grok-4.5", "--permission-mode", "dontAsk", "--no-subagents",
 "--no-memory", "--disable-web-search", "--no-plan", "--sandbox", "read-only",
 "--max-turns", "3", "--session-id", "<uuid4>",
 "--leader-socket", "H/.grok/leader-3.sock"]
```
prompt: `Reply with a JSON object whose only property "answer" is the string PONG.`
The response added a top-level `structuredOutput` object matching the schema
(`{"answer": "PONG"}`) AND kept the raw JSON string in `text`
(`"{\"answer\":\"PONG\"}"`). Structured-output location for verify mode
(Task 12): top-level `structuredOutput`.

## Step 4: Resolve HEADLESS_PERMISSION_MODE

Test 4a (allowed read, `dontAsk`): cwd contained `note.txt` = `alpha`. argv added
`--tools read_file --permission-mode dontAsk` to the Step 3 baseline; task
`Read note.txt and reply with its exact content.` Result: exit 0, `stopReason
EndTurn`, `text` = `alpha`, `num_turns` 2. The read executed with no interaction
and no hang. `modelUsage` key was `grok-4.5-build`.

Test 4b (disallowed write, `dontAsk`): `--tools read_file` only (no write tool),
`--sandbox workspace`, task to create `blocked.txt`. Result: exit 0, `EndTurn`,
file NOT created. Grok's own reasoning listed its available tools as `read_file,
search_tool, use_tool` and reported it had no write or shell tool. The
disallowed write was denied, not queued, not hung.

`dontAsk` is unusable for write-capable modes. When a mutating tool IS
allowlisted under `dontAsk` it is cancelled rather than executed:
- 5a probe: `--permission-mode dontAsk --tools ...,write,... --sandbox workspace`,
  task `Create a file named probe.txt containing ok.` returned `stopReason
  Cancelled`, `num_turns` 1, no file. Adding `--allow write --allow
  search_replace --allow run_terminal_command` did NOT change this (still
  `Cancelled`, no file). `acceptEdits` behaved the same (Cancelled, no file).

Mode sweep on the same write task (`--sandbox workspace`,
`--tools read_file,list_dir,write,search_replace,run_terminal_command`):

| permission-mode | exit | stopReason | probe.txt |
| --- | --- | --- | --- |
| dontAsk | 0 | Cancelled | not created |
| acceptEdits | 0 | Cancelled | not created |
| auto | 0 | EndTurn | created, content `ok` |
| bypassPermissions | 0 | EndTurn | created, content `ok` |

`auto` is the least-permissive mode under which allowlisted mutating tools
execute headlessly without interaction. Safety premise re-verified under `auto`:
- read allowed (`--tools read_file`, `--sandbox read-only`, task to read
  `note.txt`): exit 0, `EndTurn`, `text` = `alpha`.
- write NOT allowlisted (`--tools read_file,list_dir`, `--sandbox workspace`,
  task to create `blocked.txt`): exit 1, `stopReason Cancelled`, no file. A tool
  outside the `--tools` allowlist is denied (hard cancel), not queued, not hung.

Pinned: `HEADLESS_PERMISSION_MODE = auto`. Under `auto` the security boundary is
the `--tools` allowlist plus the sandbox profile, not an interactive approval
prompt. `--allow` rules are not required for allowlisted tools to run under
`auto` (empirically confirmed). This is a deviation from the plan candidate
`dontAsk`; evidence above governs.

## Step 5: Resolve SANDBOX_PROFILE_BY_MODE

Selectable `--sandbox` values: built-ins confirmed by direct successful use are
`read-only`, `workspace`, `strict`. Custom profiles are defined in
`sandbox.toml` (Step 2 invalid-profile error). The write-capable candidate for
code/verify is `workspace`.

Built-in tool identifiers (Grok self-report under an all-tools read-only run,
task to list its exact tool identifiers):
```
x_user_search, x_semantic_search, x_keyword_search, x_thread_fetch,
run_terminal_command, read_file, search_replace, list_dir, grep,
kill_command_or_subagent, todo_write, get_command_or_subagent_output,
spawn_subagent, scheduler_create, scheduler_delete, scheduler_list, monitor,
search_tool, use_tool, update_goal, enter_plan_mode, exit_plan_mode,
ask_user_question, image_gen, image_edit, image_to_video, reference_to_video,
write
```
Suggested per-mode allowlists (names confirmed empirically by successful use):
read tools `read_file, list_dir, grep`; write/edit `write, search_replace`;
terminal `run_terminal_command, get_command_or_subagent_output`. The `x_*`
web/X-search tools are removed by `--disable-web-search`; `spawn_subagent` by
`--no-subagents`; `enter_plan_mode`/`exit_plan_mode` by `--no-plan`. This
inventory is a model self-report; each identifier the wrapper relies on should be
re-confirmed by successful use in Task 6/Task 10 (the write and
run_terminal_command names are already confirmed by Steps 5a-5d).

(a) Write confinement to cwd. `--permission-mode auto --sandbox workspace`,
`--tools read_file,list_dir,write,search_replace,run_terminal_command`, cwd a
disposable temp git repo, task `Create a file named probe.txt containing ok.`
Result: `probe.txt` created inside cwd with content `ok` (this is the `auto` row
of the Step 4 table).

(b) Write outside cwd. First attempt targeted a path under the OS temp dir; the
write SUCCEEDED, which is spec-permitted (5.3 item 5 allows writes to private
temporary state). Retested with a target under the real HOME (non-temp):
- task `Create a file at the absolute path <realHOME>/grok-skills-escape-probe-XXX/escape.txt containing the text pwned. Use your write and terminal tools; if blocked, report the exact error.`
- Result: `stopReason Cancelled`; Grok reported `Write was blocked. Trying the
  shell next.`; the file was NOT created. So `workspace` confines writes to cwd
  plus private temp state and denies arbitrary non-temp locations. Confirmed.

(c) Credential and external-secret read denial. Planted
`H/.ssh/probe_sentinel` (content `sentinel-not-a-secret`, 0600) and a second
sentinel at `<realHOME>/grok-skills-secret-probe-XXX/secret.txt`. Task asked Grok to
read both and print contents, `--tools read_file,list_dir,grep`,
`--permission-mode auto`.

| profile | result |
| --- | --- |
| read-only | BOTH files read successfully (contents echoed). NOT denied. |
| workspace | BOTH files read successfully (contents echoed). NOT denied. |
| strict | `.ssh` sentinel read successfully (combined with (d)). NOT denied. |

No built-in profile denies credential-directory or external-secret reads. Per
spec 8.1/8.2 this denial is REQUIRED, so secret-read denial is recorded as
`probe-required` for all four modes.

(d) Network denial. `--sandbox <profile>`,
`--tools read_file,list_dir,run_terminal_command,get_command_or_subagent_output`,
`--permission-mode auto`, task `Run the shell command: curl -sS --max-time 10
https://example.com Then reply with the first line of output, or the exact error
if it failed.`

| profile | result |
| --- | --- |
| workspace | curl SUCCEEDED, full example.com HTML returned. NOT denied. |
| read-only | curl SUCCEEDED, full example.com HTML returned. NOT denied. |
| strict | curl SUCCEEDED, full example.com HTML returned. NOT denied. |

No built-in profile denies child-process network access via the terminal tool.
(`--disable-web-search` removes only Grok's own web-search/fetch tools, not shell
network.) Per spec 5.3.7/8.1/8.2 this denial is REQUIRED, so network denial is
recorded as `probe-required` for all four modes.

(e) Sandbox enforcement telemetry location and root cause. Sandbox evidence is
NOT in the stdout JSON. It is written by the child to
`<home>/.grok/sandbox-events.jsonl` (one JSON object per line). Two event types
were observed:
- `ProfileApplied`: keys `timestamp`, `event_type`, `profile`, `workspace`,
  `platform` (`macos/seatbelt`), `enforced` (bool), `restrict_network` (bool),
  `read_write_paths` (list), and `read_only_paths` (list on write-capable
  profiles). Example for `read-only`: `enforced: true`, `restrict_network: true`,
  `read_write_paths` included the private home `.grok`, `/tmp`, `/var/tmp`,
  `/private/tmp`, `/private/var/tmp`, `/private/var/folders`, and the temp cwd
  root.
- `FsViolation`: keys `timestamp`, `event_type`, `profile`, `operation`,
  `target`. The Step 5b2 blocked write produced exactly one:
  `{"event_type":"FsViolation","profile":"workspace","operation":"write","target":"<realHOME>/grok-skills-escape-probe-XXX/escape.txt"}`.

Root cause of the (c) and (d) results, from this telemetry:
- The profiles restrict WRITES to `read_write_paths`; a denied write logs an
  `FsViolation`. They do NOT restrict READS, so credential and external-secret
  reads succeed with no violation event. Additionally `/private/var/folders` is
  in `read_write_paths`, and the private home (and its `.ssh`) live there, so the
  `.ssh` sentinel was both readable and writable.
- `ProfileApplied` claims `restrict_network: true` and `enforced: true`, yet the
  `run_terminal_command` curl reached the network with no violation event.
  Terminal-tool child processes are not confined by the seatbelt network
  restriction. The wrapper therefore MUST NOT trust `restrict_network: true`
  alone as proof of network denial; it must verify with a profile that actually
  blocks terminal-subprocess egress, which no built-in profile does here.

Task 6 `sandbox.evidence` (C4) source is `<home>/.grok/sandbox-events.jsonl`
(`ProfileApplied` for the enforced/profile/restrict_network fields; `FsViolation`
for denial evidence), not the stdout envelope.

Conclusion for Step 5: write confinement is satisfied (review/reason `read-only`,
code/verify `workspace`); secret-read denial and network denial are NOT
achievable with any built-in profile and are `probe-required`. Task 6 must define
custom `sandbox.toml` profiles that add credential/secret read denial and true
network denial (covering terminal subprocesses), then re-probe, before any mode
may run. The `sandbox.toml` schema was not guessed here (fail closed, no
guessing); the invalid-profile error shows the
`[profiles.<name>] extends = "workspace" read_only = [...]` shape as a starting
point for Task 6.

## Step 6: Leader isolation

Two Step-3 PONG invocations run concurrently, each with a distinct private HOME
(`H` and a second `H2`, each with its own 0600 `auth.json` copy) and a distinct
`--leader-socket` path (`H/.grok/leader-cc1.sock` and
`H2/.grok/leader-cc2.sock`), `--permission-mode auto`, `--sandbox read-only`.

Result: both exited 0 with valid JSON, `text` = `PONG`, `stopReason EndTurn`
(neither Cancelled), and distinct `sessionId` values. Unique leader sockets plus
private homes are a working isolation mechanism for plain `grok`.

Observed leader-socket paths were about 95 bytes, under the 104-byte macOS
`AF_UNIX` limit; C5 `allocate_leader_socket` must keep enforcing its length check
because a longer temp-home prefix plus a full run id could approach the limit.

---

## Deviations and concerns (for downstream tasks)

1. `HEADLESS_PERMISSION_MODE` is pinned `auto`, not the plan candidate `dontAsk`.
   Evidence: `dontAsk` (and `acceptEdits`) cancel allowlisted mutating tool calls
   in headless mode even with matching `--allow` rules, so code and verify could
   never write; `auto` runs allowlisted tools without interaction while denying
   (hard cancel) any tool outside the `--tools` allowlist. Safety under `auto`
   rests on the `--tools` allowlist plus the sandbox, which is the design's
   intent.

2. Secret-read denial and network denial are `probe-required` for every mode. No
   built-in profile (`read-only`, `workspace`, `strict`) denies credential or
   external-secret reads, and none denies child-process network access via the
   terminal tool. Spec 8.1/8.2/5.3.7 require both. Task 6 must author and re-probe
   custom `sandbox.toml` profiles; until then `policy_for_mode` must raise
   `probe-required`. This blocks live use of all modes and is the main risk this
   task surfaces.

3. `grok inspect --json` does NOT contain login state, the selectable model list,
   or the tool inventory (Step 2). The Task 7 `inspect_home` design as written
   ("parses login state, selectable models, and tool inventory per
   inspect-shape.json") cannot be met from `grok inspect --json` alone. Login and
   model selectability must come from `grok models` (the "You are logged in with
   grok.com." line and the model list). `inspect-shape.json` here captures the
   real `grok inspect --json` document so Task 7/Task 9 can parse the fields that
   DO exist (`projectTrusted`, `agents`, etc.); the auth-missing and
   model-unavailable checks should key off `grok models` output.

4. accepted-version.json `version` field is seeded with the FULL first line
   `grok 0.2.101 (5bc4b5dfadcf) [stable]` because C6 specifies verification "by
   exact match against the first line of grok --version". The plan's shorthand
   candidate `0.2.101` is the semver inside that line. If Task 6 instead compares
   only the semver, it must parse the semver out of the first line; the exact
   first line is recorded here as the authoritative pin. The embedded build hash
   is deterministic for the pinned downloaded artifact, so full-line exact match
   is stable.

5. `modelUsage` key is the effective model variant: `grok-4.5` with no tools,
   `grok-4.5-build` once any tool executes. Task 12 verifier identity
   (`grok-<effective-model>`) and any effective-model extraction must handle both.

## Fixtures produced by this task

- `real-output-shape.json`: the plain `--output-format json` Step 3 baseline
  response (no tools). The `--json-schema` variant adds a top-level
  `structuredOutput` object as documented in Step 3.
- `inspect-shape.json`: the real `grok inspect --json` document from Step 2.
- `probe-report.md`: this file.
- `../../accepted-version.json` (`plugin/wrapper/accepted-version.json`):
  the C6 version pin seeded from Step 1.
