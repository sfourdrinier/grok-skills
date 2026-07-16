# wrapper/scripts/tests/test_progress.py

import datetime
import json
import os
import pathlib
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from groklib import platformsupport
from groklib import progress as progress_mod
from groklib.progress import LEVELS, PHASES, InvalidProgressEventError, ProgressWriter, read_events

_RUN_ID = "20260714T180924Z-abcdef"


class ProgressWriterTests(unittest.TestCase):
    """Covers the C3 JSONL progress event stream against a fully isolated temp dir."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-progress-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.progress_path = pathlib.Path(self.tmp_root) / "progress.jsonl"

    def _read_lines(self) -> "list[dict]":
        with open(self.progress_path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    @unittest.skipUnless(platformsupport.is_posix(), "POSIX file-mode assertion")
    def test_progress_file_created_0600(self) -> None:
        # dogfood-2 #10/#12: progress.jsonl carries raw model tokens (possible
        # secrets) and MUST be owner-only, matching the 0700 run dir.
        writer = ProgressWriter(_RUN_ID, self.progress_path)
        writer.emit("start", "created")
        mode = stat.S_IMODE(os.stat(self.progress_path).st_mode)
        self.assertEqual(mode, 0o600)
        # A second append does not loosen the mode.
        writer.emit("grok", "more")
        mode = stat.S_IMODE(os.stat(self.progress_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_failed_write_does_not_advance_seq(self) -> None:
        # review1 #14: a burned seq on a failed append would skip a value in the
        # strictly-monotonic C3 stream; seq is committed only after a durable write.
        writer = ProgressWriter(_RUN_ID, self.progress_path)
        writer.emit("start", "one")
        with mock.patch("groklib.progress.os.open", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                writer.emit("grok", "two")
        # After the failure, the next successful emit uses seq 2, not 3.
        event = writer.emit("grok", "three")
        self.assertEqual(event["seq"], 2)

    def test_safe_emit_degrade_log_failure_does_not_crash_the_run(self) -> None:
        # F4 (round3): when the progress write fails (OSError) AND the degrade-path
        # stderr diagnostic ALSO fails (stderr on the same failing volume),
        # safe_emit must still not raise -- the run continues degraded.
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        def _emit_oserror(*args, **kwargs):
            raise OSError("simulated progress.jsonl write failure")

        def _log_oserror(*args, **kwargs):
            raise OSError("simulated stderr write failure")

        with mock.patch.object(ProgressWriter, "emit", _emit_oserror):
            with mock.patch.object(progress_mod, "log_stderr", _log_oserror):
                result = writer.safe_emit("start", "boom")
        self.assertIsNone(result)
        self.assertTrue(writer._degraded, "the writer must be degraded even when the diagnostic also failed")
        # A subsequent call is a clean no-op (still no raise).
        self.assertIsNone(writer.safe_emit("grok", "next"))

    def test_emit_writes_valid_jsonl_matching_c3_fields(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        returned = writer.emit("start", "run created", data={"mode": "review"})

        lines = self._read_lines()
        self.assertEqual(len(lines), 1)
        on_disk = lines[0]

        self.assertEqual(on_disk["schemaVersion"], 1)
        self.assertEqual(on_disk["runId"], _RUN_ID)
        self.assertEqual(on_disk["seq"], 1)
        self.assertEqual(on_disk["phase"], "start")
        self.assertEqual(on_disk["level"], "info")
        self.assertEqual(on_disk["message"], "run created")
        self.assertEqual(on_disk["data"], {"mode": "review"})
        self.assertIn("ts", on_disk)

        self.assertEqual(returned, on_disk)

    def test_emit_omits_data_key_when_not_provided(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        returned = writer.emit("rules", "loaded rules")

        lines = self._read_lines()
        self.assertNotIn("data", lines[0])
        self.assertNotIn("data", returned)

    def test_emit_default_level_is_info(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        returned = writer.emit("start", "run created")

        self.assertEqual(returned["level"], "info")

    def test_seq_strictly_monotonic_across_emits(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        first = writer.emit("start", "run created")
        second = writer.emit("rules", "loaded rules")
        third = writer.emit("authhome", "auth home ready")

        self.assertEqual([first["seq"], second["seq"], third["seq"]], [1, 2, 3])

        lines = self._read_lines()
        self.assertEqual([line["seq"] for line in lines], [1, 2, 3])

    def test_invalid_phase_raises(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        with self.assertRaises(InvalidProgressEventError):
            writer.emit("not-a-real-phase", "boom")

        self.assertFalse(self.progress_path.exists())

    def test_invalid_level_raises(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        with self.assertRaises(InvalidProgressEventError):
            writer.emit("start", "boom", level="not-a-real-level")

        self.assertFalse(self.progress_path.exists())

    def test_data_must_be_dict_or_none(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        with self.assertRaises(InvalidProgressEventError):
            writer.emit("start", "boom", data="not-a-dict")

        with self.assertRaises(InvalidProgressEventError):
            writer.emit("start", "boom", data=["also", "not", "a", "dict"])

        self.assertFalse(self.progress_path.exists())

    def test_ts_is_utc_iso8601(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        event = writer.emit("start", "run created")

        parsed = datetime.datetime.fromisoformat(event["ts"])
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), datetime.timedelta(0))

    def test_phases_and_levels_contracts(self) -> None:
        self.assertEqual(
            PHASES,
            (
                "start",
                "rules",
                "authhome",
                "sandbox",
                "worktree",
                "grok",
                "validate",
                "finalizing",
                "cleanup",
                "done",
            ),
        )
        self.assertEqual(LEVELS, ("info", "warning", "error"))

    def test_emit_unserializable_data_raises_without_consuming_seq(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)

        first = writer.emit("start", "run created")
        self.assertEqual(first["seq"], 1)

        with self.assertRaises(InvalidProgressEventError):
            writer.emit("rules", "boom", data={"x": object()})

        second = writer.emit("authhome", "auth home ready")
        self.assertEqual(second["seq"], 2)

        lines = self._read_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([line["seq"] for line in lines], [1, 2])


class ReadEventsTests(unittest.TestCase):
    """Covers the C3 reader contract: skip torn/invalid lines, never raise for them."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-progress-read-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.progress_path = pathlib.Path(self.tmp_root) / "progress.jsonl"

    def test_read_events_skips_torn_trailing_line_with_warning(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)
        writer.emit("start", "run created")
        writer.emit("rules", "loaded rules")

        # Simulate a torn trailing write: a partial JSON line with no
        # closing brace and no trailing newline, as C3 describes for a
        # concurrent status read racing an in-flight append.
        with open(self.progress_path, "a", encoding="utf-8") as handle:
            handle.write('{"schemaVersion": 1, "runId": "' + _RUN_ID + '", "seq": 3, "phase": "auth')

        events, warnings = read_events(self.progress_path)

        self.assertEqual(len(events), 2)
        self.assertEqual(len(warnings), 1)
        self.assertEqual([event["seq"] for event in events], [1, 2])

    def test_read_events_skips_torn_multibyte_trailing_line_with_warning(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)
        writer.emit("start", "run created")
        writer.emit("rules", "loaded rules")

        # Simulate a torn trailing write that ends partway through a 2-byte
        # UTF-8 sequence (0xC3 is a lead byte that requires a continuation
        # byte 0x80-0xBF; here none follows), with no trailing newline. A
        # whole-file strict-UTF-8 decode would raise UnicodeDecodeError; the
        # per-line byte reader must instead skip this line with a warning.
        with open(self.progress_path, "ab") as handle:
            handle.write(b'{"phase": "grok", "message": "caf\xc3')

        events, warnings = read_events(self.progress_path)

        self.assertEqual(len(events), 2)
        self.assertEqual(len(warnings), 1)
        self.assertEqual([event["seq"] for event in events], [1, 2])

    def test_read_events_missing_file_returns_empty_with_warning(self) -> None:
        missing_path = pathlib.Path(self.tmp_root) / "does-not-exist.jsonl"

        events, warnings = read_events(missing_path)

        self.assertEqual(events, [])
        self.assertEqual(len(warnings), 1)

    def test_read_events_valid_file_returns_no_warnings(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)
        writer.emit("start", "run created")
        writer.emit("done", "finished", level="info")

        events, warnings = read_events(self.progress_path)

        self.assertEqual(len(events), 2)
        self.assertEqual(warnings, [])

    def test_read_events_skips_non_object_json_line_with_warning(self) -> None:
        writer = ProgressWriter(_RUN_ID, self.progress_path)
        writer.emit("start", "run created")

        with open(self.progress_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps([1, 2, 3]) + "\n")

        events, warnings = read_events(self.progress_path)

        self.assertEqual(len(events), 1)
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
