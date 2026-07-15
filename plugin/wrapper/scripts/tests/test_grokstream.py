# wrapper/scripts/tests/test_grokstream.py

import json
import pathlib
import unittest

from groklib import grokstream
from groklib import grokcli_output

_FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"


def _base_payload() -> dict:
    """The Task 0 `--output-format json` success blob the streaming path must stay equivalent to."""
    return json.loads((_FIXTURES_DIR / "real-output-shape.json").read_text(encoding="utf-8"))


class TryParseStreamLineTests(unittest.TestCase):
    """try_parse_stream_line returns an object for a JSON object line, None otherwise."""

    def test_parses_json_object_line(self) -> None:
        obj = grokstream.try_parse_stream_line('{"type": "thought", "data": "hi"}\n')
        self.assertEqual(obj, {"type": "thought", "data": "hi"})

    def test_non_json_line_returns_none(self) -> None:
        self.assertIsNone(grokstream.try_parse_stream_line("not json {"))

    def test_non_object_json_returns_none(self) -> None:
        self.assertIsNone(grokstream.try_parse_stream_line("[1, 2, 3]"))

    def test_blank_line_returns_none(self) -> None:
        self.assertIsNone(grokstream.try_parse_stream_line("   \n"))


class StreamAssemblerTests(unittest.TestCase):
    """StreamAssembler concatenates text tokens and assembles a json-blob-equivalent parsed dict."""

    def _feed_stream_of(self, payload: dict) -> grokstream.StreamAssembler:
        """Replay a json blob as the T2-0.0 stream shape (thought/text tokens + terminal end)."""
        assembler = grokstream.StreamAssembler()
        thought = payload.get("thought")
        if isinstance(thought, str):
            for character in thought:
                assembler.feed({"type": "thought", "data": character})
        text = payload.get("text")
        if isinstance(text, str):
            # split into a few chunks to exercise concatenation
            mid = max(1, len(text) // 2)
            for chunk in (text[:mid], text[mid:]):
                if chunk:
                    assembler.feed({"type": "text", "data": chunk})
        end = {"type": "end"}
        for key, value in payload.items():
            if key in ("thought", "text"):
                continue
            end[key] = value
        assembler.feed(end)
        return assembler

    def test_build_parsed_is_envelope_equivalent_to_json_blob(self) -> None:
        payload = _base_payload()
        assembler = self._feed_stream_of(payload)
        self.assertTrue(assembler.has_terminal)
        parsed = assembler.build_parsed()

        # Every envelope-relevant field the wrapper reads must match the json blob.
        stream_fields = grokcli_output.extract_result_fields(parsed)
        blob_fields = grokcli_output.extract_result_fields(payload)
        self.assertEqual(stream_fields, blob_fields)
        # usage + num_turns (read by grok_usage_response_fields) must match too.
        self.assertEqual(parsed.get("usage"), payload.get("usage"))
        self.assertEqual(parsed.get("num_turns"), payload.get("num_turns"))
        # text is reconstructed from the streamed chunks.
        self.assertEqual(parsed.get("text"), payload.get("text"))

    def test_terminal_carries_through_every_key_except_type(self) -> None:
        assembler = grokstream.StreamAssembler()
        assembler.feed({"type": "text", "data": "answer"})
        assembler.feed(
            {
                "type": "end",
                "stopReason": "EndTurn",
                "structuredOutput": {"answer": "PONG"},
                "changedFiles": ["pkg/x.txt"],
            }
        )
        parsed = assembler.build_parsed()
        self.assertNotIn("type", parsed)
        self.assertEqual(parsed["structuredOutput"], {"answer": "PONG"})
        self.assertEqual(parsed["changedFiles"], ["pkg/x.txt"])
        self.assertEqual(parsed["text"], "answer")

    def test_empty_stream_has_no_terminal_and_saw_nothing(self) -> None:
        assembler = grokstream.StreamAssembler()
        self.assertFalse(assembler.saw_any_line)
        self.assertFalse(assembler.has_terminal)

    def test_torn_stream_without_terminal_is_flagged(self) -> None:
        assembler = grokstream.StreamAssembler()
        assembler.feed({"type": "thought", "data": "reasoning"})
        assembler.feed({"type": "text", "data": "partial"})
        self.assertTrue(assembler.saw_any_line)
        self.assertFalse(assembler.has_terminal)

    def test_unknown_event_type_is_classified_other_not_rejected(self) -> None:
        assembler = grokstream.StreamAssembler()
        event = assembler.feed({"type": "tool_call", "data": {"name": "grep"}})
        self.assertEqual(event.kind, "other")
        self.assertEqual(event.event_type, "tool_call")
        self.assertTrue(assembler.saw_any_line)


class ProgressCoalescerTests(unittest.TestCase):
    """ProgressCoalescer batches same-kind tokens and flushes on kind change / budget / end."""

    def test_same_kind_tokens_coalesce_until_flush(self) -> None:
        coalescer = grokstream.ProgressCoalescer(char_budget=1000)
        self.assertIsNone(coalescer.feed("thought", "a"))
        self.assertIsNone(coalescer.feed("thought", "b"))
        payload = coalescer.flush()
        self.assertEqual(payload, {"event": "thought", "chars": 2, "text": "ab"})

    def test_kind_change_flushes_previous_batch(self) -> None:
        coalescer = grokstream.ProgressCoalescer(char_budget=1000)
        self.assertIsNone(coalescer.feed("thought", "abc"))
        flushed = coalescer.feed("text", "X")
        self.assertEqual(flushed, {"event": "thought", "chars": 3, "text": "abc"})
        # the text batch is still buffered until flush
        self.assertEqual(coalescer.flush(), {"event": "text", "chars": 1, "text": "X"})

    def test_char_budget_forces_flush(self) -> None:
        coalescer = grokstream.ProgressCoalescer(char_budget=3)
        self.assertIsNone(coalescer.feed("thought", "ab"))
        payload = coalescer.feed("thought", "cd")
        self.assertEqual(payload, {"event": "thought", "chars": 4, "text": "abcd"})
        self.assertIsNone(coalescer.flush())

    def test_flush_of_empty_buffer_is_none(self) -> None:
        self.assertIsNone(grokstream.ProgressCoalescer().flush())


if __name__ == "__main__":
    unittest.main()
