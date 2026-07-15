# wrapper/scripts/groklib/citations.py
#
# Citation capture for web-grounded runs (Wave 1 C-A5). Pure parsers over
# final text and optional stream-ish event dicts. No network. Prefer a
# machine-parseable Sources: block in the model answer; also harvest URL-shaped
# strings from stream event payloads when present.

import re
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from groklib.envelope import redact_secret_value_text

GROUNDING_REQUESTED_NO_SOURCES = "grounding-requested-no-sources"

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "code",
        "password",
        "passwd",
        "secret",
        "signature",
        "sig",
        "apikey",
        "api_key",
        "key",
        "auth",
        "authorization",
        "session",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
    }
)

# One Sources line: - <url> | <title> | <what-it-grounded>
_SOURCES_LINE = re.compile(
    r"^\s*[-*]\s+"
    r"(?P<url>https?://\S+?)"
    r"(?:\s*\|\s*(?P<title>[^|\n]+?))?"
    r"(?:\s*\|\s*(?P<grounded>[^\n]+))?"
    r"\s*$",
    re.MULTILINE,
)
_SOURCES_HEADER = re.compile(r"(?im)^\s*sources?\s*:\s*$")
_URL_IN_TEXT = re.compile(r"https?://[^\s\]\)\"'<>]+")


def _clean_url(url: str) -> str:
    return url.rstrip(").,;]")


def scrub_citation_url(url: str) -> str:
    """Strip userinfo and sensitive query values from a citation URL."""
    cleaned = _clean_url((url or "").strip())
    if not cleaned:
        return cleaned
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return redact_secret_value_text(cleaned)
    # Drop credentials from user:pass@host
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _SENSITIVE_QUERY_KEYS or any(
            token in key.lower() for token in ("token", "secret", "password", "signature", "apikey")
        ):
            query_pairs.append((key, "[redacted]"))
        else:
            query_pairs.append((key, value))
    rebuilt = urlunsplit((parts.scheme, netloc, parts.path, urlencode(query_pairs), parts.fragment))
    return redact_secret_value_text(rebuilt)


def _citation(url: str, title: str = "", grounded: str = "") -> Dict[str, str]:
    return {
        "url": scrub_citation_url(url),
        "title": redact_secret_value_text((title or "").strip()),
        "grounded": redact_secret_value_text((grounded or "").strip()),
    }


def parse_sources_block(text: str) -> List[Dict[str, str]]:
    """Parse an instructed trailing Sources: block into Citation dicts."""
    if not isinstance(text, str) or not text.strip():
        return []
    header = _SOURCES_HEADER.search(text)
    if header is None:
        # Still accept loose "- url | title | note" lines anywhere.
        block = text
    else:
        block = text[header.end() :]
    found: List[Dict[str, str]] = []
    seen = set()
    for match in _SOURCES_LINE.finditer(block):
        url = _clean_url(match.group("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        found.append(
            _citation(
                url,
                (match.group("title") or "").strip(),
                (match.group("grounded") or "").strip(),
            )
        )
    return found


def parse_citations_from_events(events: Optional[Sequence[object]]) -> List[Dict[str, str]]:
    """Harvest URL citations from stream-like event dicts (best-effort)."""
    if not events:
        return []
    found: List[Dict[str, str]] = []
    seen = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        chunks: List[str] = []
        for key in ("url", "href", "source", "title", "text", "content", "message"):
            value = event.get(key)
            if isinstance(value, str):
                chunks.append(value)
        data = event.get("data")
        if isinstance(data, dict):
            for key in ("url", "href", "source", "title", "text", "content"):
                value = data.get(key)
                if isinstance(value, str):
                    chunks.append(value)
        blob = "\n".join(chunks)
        title_hint = ""
        if isinstance(event.get("title"), str):
            title_hint = event["title"]
        elif isinstance(data, dict) and isinstance(data.get("title"), str):
            title_hint = data["title"]
        for url in _URL_IN_TEXT.findall(blob):
            clean = _clean_url(url)
            if not clean or clean in seen:
                continue
            seen.add(clean)
            found.append(_citation(clean, title_hint, "stream-source"))
    return found


def collect_citations(
    final_text: Optional[str],
    events: Optional[Sequence[object]] = None,
) -> List[Dict[str, str]]:
    """Prefer Sources-block parse; merge unique stream URLs after."""
    primary = parse_sources_block(final_text or "")
    seen = {c["url"] for c in primary}
    for extra in parse_citations_from_events(events):
        if extra["url"] not in seen:
            primary.append(extra)
            seen.add(extra["url"])
    return primary


def citations_for_run(
    *,
    web_access: bool,
    final_text: Optional[str],
    events: Optional[Sequence[object]] = None,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Return (citations, extra_warnings) for envelope assembly.

    When web was requested and zero citations were found, emit
    ``grounding-requested-no-sources`` (C-A2 / D-W1-FAILCLOSED-GROUND).
    """
    if not web_access:
        return [], []
    citations = collect_citations(final_text, events)
    warnings: List[str] = []
    if not citations:
        warnings.append(GROUNDING_REQUESTED_NO_SOURCES)
    return citations, warnings
