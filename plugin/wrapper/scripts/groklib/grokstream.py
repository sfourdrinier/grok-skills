# wrapper/scripts/groklib/grokstream.py
#
# Pure parsing and assembly of Grok's `--output-format streaming-json` stdout
# (T2-0 / decision D-STREAM). The wrapper switched execute() from the single
# `--output-format json` blob to the incremental stream so every mode (incl.
# write-capable code/verify) can relay Grok's live thought/text tokens into the
# run's progress.jsonl AS THEY ARRIVE, while still assembling the SAME final
# result the json blob produced.
#
# Grounded by the T2-0.0 live probe (plugin/references/streaming-json-events.md):
# the stdout stream is newline-delimited JSON objects, each with a "type":
#   - {"type":"thought","data":"<token>"}  Grok's live reasoning tokens
#   - {"type":"text","data":"<token>"}     the final answer, token by token
#   - {"type":"end", ...}                  the terminal result: stopReason,
#         sessionId, requestId, num_turns, usage, modelUsage, and (schema runs)
#         structuredOutput. It does NOT carry the assembled `text`.
# An unknown-but-valid object type is surfaced as a generic progress event, never
# a failure, so a future Grok event class does not break the wrapper.
#
# This module has ZERO subprocess / filesystem / progress-writing concerns of
# its own (grokcli.execute owns the child process and the ProgressWriter); it
# only turns raw lines into classified facts and assembles the equivalent parsed
# result dict, so it is unit-testable directly against the probe-captured shapes.
#
# stdout discipline: this module NEVER writes to stdout. Diagnostics go to
# stderr through the shared log helper.

import json
from typing import Dict, List, Optional

from groklib import log_stderr

# The three stream event types the T2-0.0 probe captured. Any other object type
# is treated as a generic ("other") event and surfaced, not rejected.
STREAM_THOUGHT_TYPE = "thought"
STREAM_TEXT_TYPE = "text"
STREAM_TERMINAL_TYPE = "end"

# Progress coalescing: consecutive same-kind token events are batched into one
# progress event, flushed on a kind change, on this character budget, or at end
# of stream, so a token-level stream (thousands of one-word events) does not
# explode progress.jsonl while still surfacing live-enough updates for the relay.
_COALESCE_CHAR_BUDGET = 480


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "grokstream" component prefix."""
    log_stderr("grokstream", function, message)


def try_parse_stream_line(raw: str) -> Optional[Dict[str, object]]:
    """Parse one streaming-json line into a JSON object, or return None when it is not one.

    A non-JSON line or a JSON value that is not an object returns None (logged
    with structural context only, never the line content) so the caller can
    classify the run as ``output-malformed`` after draining the rest of the
    stream. The caller is responsible for skipping blank lines before calling.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        _log("try_parse_stream_line", "stream line was not valid JSON: {}".format(exc))
        return None
    if not isinstance(parsed, dict):
        _log("try_parse_stream_line", "stream line JSON was a {} not an object".format(type(parsed).__name__))
        return None
    return parsed


class StreamEvent:
    """One classified stream event: its kind, and (for token events) the token text.

    kind is one of "thought" | "text" | "end" | "other". ``text`` is the token
    string for thought/text events (empty otherwise). ``event_type`` is the raw
    ``type`` field, used to describe an unknown ("other") event.
    """

    def __init__(self, kind: str, text: str, event_type: Optional[str]) -> None:
        self.kind = kind
        self.text = text
        self.event_type = event_type


class StreamAssembler:
    """Accumulates the streamed text tokens and captures the terminal ``end`` event.

    ``feed`` classifies one already-parsed stream object, appends text-token
    data to the running answer, and records the terminal event. ``build_parsed``
    then assembles the dict equivalent to a single ``--output-format json`` blob:
    every key of the terminal event except ``type``, plus
    ``text`` = the concatenation of every ``text`` token in arrival order. That
    dict feeds the UNCHANGED extract_result_fields / envelope builders, so the
    final envelope is equivalent to the pre-D-STREAM json-path envelope.
    """

    def __init__(self) -> None:
        self._text_parts: List[str] = []
        self._terminal: Optional[Dict[str, object]] = None
        self._saw_any_line = False

    @property
    def saw_any_line(self) -> bool:
        """True once at least one stream object has been fed (an empty stream stays False)."""
        return self._saw_any_line

    @property
    def has_terminal(self) -> bool:
        """True once the terminal ``end`` event has been seen."""
        return self._terminal is not None

    def feed(self, obj: Dict[str, object]) -> StreamEvent:
        """Classify one parsed stream object, accumulating text and capturing the terminal event."""
        self._saw_any_line = True
        event_type = obj.get("type")
        if event_type == STREAM_TERMINAL_TYPE:
            self._terminal = obj
            return StreamEvent("end", "", STREAM_TERMINAL_TYPE)
        if event_type == STREAM_TEXT_TYPE:
            data = obj.get("data")
            token = data if isinstance(data, str) else ""
            if token:
                self._text_parts.append(token)
            return StreamEvent("text", token, STREAM_TEXT_TYPE)
        if event_type == STREAM_THOUGHT_TYPE:
            data = obj.get("data")
            token = data if isinstance(data, str) else ""
            return StreamEvent("thought", token, STREAM_THOUGHT_TYPE)
        return StreamEvent("other", "", event_type if isinstance(event_type, str) else None)

    def build_parsed(self) -> Dict[str, object]:
        """Assemble the parsed-result dict equivalent to a single json blob (terminal required).

        The whole terminal event is carried through (minus ``type``) so any
        field Grok places on ``end`` - stopReason, sessionId, requestId,
        num_turns, usage, modelUsage, structuredOutput, and any future or
        defensive change key - reaches the envelope builders exactly as the json
        blob would have carried it. ``text`` is (re)set to the concatenation of
        the streamed text tokens, which the terminal event itself does not carry.
        Callers MUST verify ``has_terminal`` before calling; a missing terminal
        is a torn/incomplete stream that the caller classifies as
        ``output-malformed``.
        """
        if self._terminal is None:
            raise ValueError("build_parsed called before a terminal event was seen")
        parsed: Dict[str, object] = {key: value for key, value in self._terminal.items() if key != "type"}
        parsed["text"] = "".join(self._text_parts)
        return parsed


class ProgressCoalescer:
    """Batches consecutive same-kind token events into bounded progress payloads.

    Consecutive ``thought`` (or ``text``) tokens are concatenated until the kind
    changes or the character budget is crossed, at which point ``feed`` returns a
    payload for the caller to emit. ``flush`` drains any remaining buffered text
    at end of stream. This bounds the number of progress.jsonl events a
    token-level stream produces while keeping updates live enough for the relay.
    """

    def __init__(self, char_budget: int = _COALESCE_CHAR_BUDGET) -> None:
        self._char_budget = char_budget
        self._kind: Optional[str] = None
        self._buffer: List[str] = []
        self._length = 0

    def _payload(self) -> Optional[Dict[str, object]]:
        """Build and clear the current buffer's progress payload, or None when empty."""
        if self._kind is None or not self._buffer:
            self._kind = None
            self._buffer = []
            self._length = 0
            return None
        text = "".join(self._buffer)
        payload: Dict[str, object] = {"event": self._kind, "chars": len(text), "text": text}
        self._kind = None
        self._buffer = []
        self._length = 0
        return payload

    def feed(self, kind: str, token: str) -> Optional[Dict[str, object]]:
        """Add one token of the given kind; return a flush payload when the batch closes.

        A kind change flushes the previous batch (returned) and starts a new one
        with ``token``. Same-kind tokens accumulate until the character budget is
        crossed, which flushes and returns the batch. Returns None while still
        accumulating.
        """
        if self._kind is not None and kind != self._kind:
            previous = self._payload()
            self._kind = kind
            self._buffer = [token]
            self._length = len(token)
            return previous
        self._kind = kind
        self._buffer.append(token)
        self._length += len(token)
        if self._length >= self._char_budget:
            return self._payload()
        return None

    def flush(self) -> Optional[Dict[str, object]]:
        """Return the remaining buffered batch as a payload (or None when empty)."""
        return self._payload()
