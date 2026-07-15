# wrapper/scripts/tests/test_citations.py

import unittest

from groklib import citations


class CitationsParserTests(unittest.TestCase):
    def test_parse_sources_block(self) -> None:
        text = (
            "Findings here.\n\n"
            "Sources:\n"
            "- https://example.com/a | Title A | claims CVE\n"
            "- https://example.com/b | Title B | API shape\n"
        )
        found = citations.parse_sources_block(text)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]["url"], "https://example.com/a")
        self.assertEqual(found[0]["title"], "Title A")
        self.assertEqual(found[0]["grounded"], "claims CVE")

    def test_parse_events_urls(self) -> None:
        events = [
            {"type": "tool", "data": {"url": "https://docs.example/x", "title": "Docs"}},
            {"text": "see https://news.example/y for more"},
        ]
        found = citations.parse_citations_from_events(events)
        urls = {c["url"] for c in found}
        self.assertIn("https://docs.example/x", urls)
        self.assertIn("https://news.example/y", urls)

    def test_grounding_warning_when_empty(self) -> None:
        cites, warnings = citations.citations_for_run(web_access=True, final_text="no sources")
        self.assertEqual(cites, [])
        self.assertEqual(warnings, [citations.GROUNDING_REQUESTED_NO_SOURCES])

    def test_no_warning_when_web_off(self) -> None:
        cites, warnings = citations.citations_for_run(web_access=False, final_text="no sources")
        self.assertEqual(cites, [])
        self.assertEqual(warnings, [])

    def test_collect_prefers_block(self) -> None:
        text = "Sources:\n- https://only.example/z | Z | note\n"
        events = [{"url": "https://stream.example/a"}]
        found = citations.collect_citations(text, events)
        self.assertEqual(found[0]["url"], "https://only.example/z")
        self.assertEqual(found[1]["url"], "https://stream.example/a")


class WebDefaultsTests(unittest.TestCase):
    def test_defaults_table(self) -> None:
        from groklib.web_defaults import resolve_web_access

        self.assertFalse(resolve_web_access("reason", None))
        self.assertFalse(resolve_web_access("review", None))
        self.assertFalse(resolve_web_access("code", None))
        self.assertFalse(resolve_web_access("verify", None))
        self.assertTrue(resolve_web_access("reason", True))
        self.assertTrue(resolve_web_access("review", True))
        self.assertFalse(resolve_web_access("verify", True))

    def test_scrub_citation_url_userinfo_and_token_query(self) -> None:
        from groklib.citations import scrub_citation_url

        scrubbed = scrub_citation_url(
            "https://user:s3cret@example.com/cb?access_token=abc123&ok=1"
        )
        self.assertNotIn("s3cret", scrubbed)
        self.assertNotIn("abc123", scrubbed)
        self.assertIn("example.com", scrubbed)
        self.assertIn("ok=1", scrubbed)


if __name__ == "__main__":
    unittest.main()
