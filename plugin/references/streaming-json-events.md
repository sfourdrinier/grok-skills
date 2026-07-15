<!-- plugin/references/streaming-json-events.md -->

# Grok `--output-format streaming-json` event set (T2-0.0 grounding)

Captured 2026-07-14 via two isolated live probes (v1 Task-0 discipline: private
HOME 0700, 0600 auth copy, unique `--leader-socket`, isolated temp cwd OUTSIDE
the host checkout, disposable git repo, auth removed FIRST in cleanup via
`os.remove`, no auth printed). Grok binary `grok 0.2.101 (5bc4b5dfadcf) [stable]`
(matches the accepted-version pin). No secrets are recorded here; the only
content shown is Grok's own model output tokens.

## Probe 1: tool-using run (write-capable)

Command (single-shot, the SAME hardened invocation the v1 wrapper builds, with
`--output-format streaming-json` swapped in for `--output-format json`):

```
grok --prompt-file <prompt> --verbatim --cwd <disposable-repo>
     --model grok-4.5 --output-format streaming-json --permission-mode auto
     --tools read_file,write,search_replace,run_terminal_command,list_dir
     --no-subagents --no-memory --disable-web-search --no-plan
     --sandbox workspace --max-turns 8 --leader-socket <unique>
```

Prompt: "Create a file named hello.txt containing exactly the text hi, then read
it back and tell me its contents." (the model actually created and read the file;
exit 0, 57.96s, 77 stdout lines.)

### Event types observed (the COMPLETE set)

| `type`     | count | shape                                        |
|------------|-------|----------------------------------------------|
| `thought`  | 60    | `{"type":"thought","data":"<token>"}`        |
| `text`     | 16    | `{"type":"text","data":"<token>"}`           |
| `end`      | 1     | terminal result (see below)                  |

Ordering in this run: all `thought` tokens, then all `text` tokens, then a
single `end`. (Across multi-turn runs the thought/text groups can interleave;
the parser must not assume a fixed order - it concatenates every `text` chunk in
arrival order and treats the single `end` as terminal.)

KEY FINDING: there are NO explicit `tool_call` / `tool_result` stream events.
Grok performed real tool work (wrote and read a file) but surfaced it only
through the `thought` token stream and the `text` answer. So the stream event
vocabulary on stdout is exactly `{thought, text, end}`. stderr was empty.

### `thought` / `text` events

Both are token-level: `data` is a short string fragment (often a single word or
punctuation). Reconstruct the final message text by concatenating every `text`
event's `data` in arrival order. Example reconstruction from this run:

```
"Created `hello.txt` and read it back.\n\n**Contents:** `hi`"
```

`thought` tokens are Grok's live chain-of-thought (reasoning tokens); they are
NOT consumed by the envelope (the v1 `--output-format json` blob's `thought`
field is likewise never read by extract_result_fields / the envelope builder).

### Terminal `end` event (verbatim)

```json
{
  "type": "end",
  "stopReason": "EndTurn",
  "sessionId": "22222222-2222-4222-8222-222222222222",
  "requestId": "a8ea173b-1a28-48c6-bf50-11f0e2c1a453",
  "num_turns": 3,
  "usage": {
    "input_tokens": 9448,
    "cache_read_input_tokens": 20608,
    "output_tokens": 208,
    "reasoning_tokens": 60,
    "total_tokens": 30264
  },
  "modelUsage": {
    "grok-4.5-build": {
      "inputTokens": 9448,
      "outputTokens": 208,
      "cacheReadInputTokens": 20608,
      "modelCalls": 3
    }
  }
}
```

## Probe 2: schema run (`--json-schema` + `--output-format streaming-json`)

Question answered: does `--json-schema` coexist with streaming-json, and where
does `structuredOutput` land? Ran the SAME schema twice - once with
`--output-format json` (baseline), once with `--output-format streaming-json`.

Schema: `{"type":"object","required":["answer"],"properties":{"answer":{"type":"string"}}}`

Baseline `--output-format json` blob top-level keys:
`[modelUsage, num_turns, requestId, sessionId, stopReason, structuredOutput, text, thought, usage]`
with `structuredOutput = {"answer":"PONG"}` and `text = "{\"answer\":\"PONG\"}"`.

`--output-format streaming-json` (both flags together, exit 0): type counts
`{thought:52, text:7, end:1}`. The terminal `end` event carried
`structuredOutput` directly:

```json
{
  "type": "end",
  "stopReason": "EndTurn",
  "sessionId": "33333333-3333-4333-8333-333333333333",
  "requestId": "41f78aaa-b1e3-40e1-81a4-e52d9ec79be5",
  "num_turns": 2,
  "structuredOutput": {"answer": "PONG"},
  "usage": { "input_tokens": 18525, "cache_read_input_tokens": 3840,
             "output_tokens": 99, "reasoning_tokens": 85, "total_tokens": 22464 },
  "modelUsage": { "grok-4.5": { "inputTokens": 18525, "outputTokens": 99,
                                "cacheReadInputTokens": 3840, "modelCalls": 2 } }
}
```

FINDING: `--json-schema` and `--output-format streaming-json` compose cleanly.
The `end` event carries `structuredOutput` for schema runs (absent for
non-schema runs). So EVERY mode - including the schema-driven `verify`
(elicit_schema) and structured `review`/`reason` - can stream while still
delivering the structured result through the terminal event.

## DECISION POINT resolution (does the terminal event carry the full result?)

The v1 wrapper builds its envelope from these fields of the parsed
`--output-format json` blob (see `grokcli_output.extract_result_fields`,
`modes/_shared.grok_usage_response_fields`, `modes/_shared._grok_reported_changes`):
`stopReason, sessionId, requestId, modelUsage, usage, num_turns, text,
structuredOutput`, plus a defensive scan for file-change keys
(`changedFiles`/`fileChanges`/...).

The `end` event carries ALL of these EXCEPT `text` (which is streamed as `text`
chunks) and, of course, the never-actually-emitted defensive change keys. So the
terminal event DOES carry the full result the wrapper needs, PROVIDED the parser
also concatenates the streamed `text` chunks. No separate `--output-format json`
summary is required.

Assembly rule (implemented in `groklib/grokstream.py`): the equivalent parsed
result dict = every key of the `end` event except `type`, PLUS
`text = "".join(all text-event data)`. Feeding that dict through the UNCHANGED
`extract_result_fields` / envelope builders yields the SAME envelope the
`--output-format json` path produced. This is proven by an envelope-equivalence
test (fixture run) in `tests/test_grokcli.py` and `tests/test_grokstream.py`.

## Fail-closed mapping against the stream

- empty stream, exit 0        -> output-missing
- empty stream, exit != 0     -> cli-failure (stderr captured)
- a non-JSON / non-object line, OR lines but NO terminal `end` (torn stream)
                              -> output-malformed
- `end.stopReason` Cancelled  -> cancelled (wins over exit code)
- `end.stopReason` max-turn / num_turns>=max_turns -> turn-exhaustion
- structured output failing the caller schema -> schema-mismatch
- any other nonzero exit with a parsed terminal -> cli-failure
- wall-clock exceeded          -> timeout (process TREE killed via
                                  platformsupport.kill_process_tree, unchanged)
