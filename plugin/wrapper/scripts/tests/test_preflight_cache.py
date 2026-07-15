# wrapper/scripts/tests/test_preflight_cache.py

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from groklib import preflight_cache


class PreflightCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.state_root = pathlib.Path(self._tmpdir.name)
        self._patch = mock.patch.object(preflight_cache.runstate, "state_root", return_value=self.state_root)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_missing_cache_is_invalid(self) -> None:
        self.assertFalse(preflight_cache.is_valid("1.0.0"))

    def test_write_and_hit(self) -> None:
        preflight_cache.write_ok("1.2.3", checked_at_ms=1_000_000)
        self.assertTrue(preflight_cache.is_valid("1.2.3", now_ms=1_000_000 + 60_000))

    def test_version_mismatch_fails_closed(self) -> None:
        preflight_cache.write_ok("1.2.3", checked_at_ms=1_000_000)
        self.assertFalse(preflight_cache.is_valid("9.9.9", now_ms=1_000_000 + 1_000))

    def test_stale_ttl_fails_closed(self) -> None:
        preflight_cache.write_ok("1.2.3", checked_at_ms=1_000_000)
        past = 1_000_000 + preflight_cache.DEFAULT_TTL_MS + 1
        self.assertFalse(preflight_cache.is_valid("1.2.3", now_ms=past))

    def test_malformed_json_fails_closed(self) -> None:
        path = self.state_root / preflight_cache.CACHE_FILENAME
        path.write_text("{not-json", encoding="utf-8")
        self.assertIsNone(preflight_cache.load_cache())
        self.assertFalse(preflight_cache.is_valid("1.0.0"))

    def test_ok_false_fails_closed(self) -> None:
        path = self.state_root / preflight_cache.CACHE_FILENAME
        path.write_text(
            json.dumps({"version": "1", "checkedAtMs": 1, "ok": False}),
            encoding="utf-8",
        )
        self.assertFalse(preflight_cache.is_valid("1", now_ms=1))

    def test_invalidate_removes_file(self) -> None:
        preflight_cache.write_ok("1.0.0")
        self.assertTrue((self.state_root / preflight_cache.CACHE_FILENAME).is_file())
        preflight_cache.invalidate()
        self.assertFalse((self.state_root / preflight_cache.CACHE_FILENAME).is_file())


if __name__ == "__main__":
    unittest.main()
