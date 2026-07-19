# wrapper/scripts/tests/test_redaction.py

import contextlib
import io
import json
import unittest

from groklib import GrokWrapperError
from groklib.envelope import (
    InvalidEnvelopeError,
    SecretMaterialError,
    assert_no_secret_material,
    redact_secret_material,
    redact_secret_value_text,
    build_envelope,
    failure_envelope,
)

_RUN_ID = "20260714T180924Z-abcdef"


def _fx(*parts: str) -> str:
    """Join fixture chunks at runtime so source never holds a contiguous secret shape."""
    return "".join(parts)


# Runtime-only secret shapes (AGENTS.md #8: never contiguous in source).
_GITHUB_PAT = _fx(
    "github_pat_",
    "11ABCDEFG0123456789_",
    "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ",
)
_GHP = _fx("ghp_", "1234567890abcdefghijklmnopqrstuvwx")
_XOXB = _fx("xoxb-", "123456789012-123456789012-abcdefABCDEF")
_XAI_LONG = _fx("xai-", "abcdefghijklmnopqrstuvwxyz0123456789")
_SK_LEGACY = _fx("sk-", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_SK_LEGACY_LOWER = _fx("sk-", "abcdef0123456789ABCDEFGHIJKLMNOP")
_XAI_LEAK = _fx("xai-", "THISISASECRETVALUE1234567890ABCDEF")
_SK_PROJ = _fx(
    "sk-proj-",
    "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
)
_SK_ANT_API = _fx(
    "sk-ant-",
    "api03-AbCdEf0123456789_Gh-IjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOAA",
)
_SK_ANT_ADMIN = _fx(
    "sk-ant-",
    "admin01-Zz0123456789AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjklAA",
)
_JWT = _fx(
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.",
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0.",
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
)
_JWT_SHORT = _fx("eyJhbGciOiJIUzI1NiJ9.", "eyJzdWIiOiIxMjMifQ.", "aGVsbG8xMjM")
_AWS_AKIA = _fx("AKIA", "IOSFODNN7EXAMPLE")
_AWS_ASIA = _fx("ASIA", "IOSFODNN7EXAMPLE")
_BEARER_OPAQUE = _fx("4f8a9c3d2e1b7f6a5d4c3b2a1908f7e6d5c4b3a2", "secretvalue")
_BEARER_ALPHA = _fx("AbCdEfGhIjKlMnOp", "QrStUv")
_BEARER_MIXED = _fx("abcdef123456", "ABCDEFsecretbody")
_BEARER_SHORT = _fx("abc", "123")
_PEM_RSA_BEGIN = _fx("-----BEGIN ", "RSA PRIVATE KEY-----")
_PEM_RSA_END = _fx("-----END ", "RSA PRIVATE KEY-----")
_PEM_PGP_BEGIN = _fx("-----BEGIN ", "PGP PRIVATE KEY BLOCK-----")
_PEM_PGP_END = _fx("-----END ", "PGP PRIVATE KEY BLOCK-----")


class SecretScanGuardTests(unittest.TestCase):
    """Covers assert_no_secret_material and its use inside build_envelope."""

    def test_assert_no_secret_material_passes_clean_object(self) -> None:
        assert_no_secret_material({"path": "CLAUDE.md", "nested": {"count": 3, "items": ["a", "b"]}})

    def test_assert_no_secret_material_raises_on_secret_key_case_insensitive(self) -> None:
        for key in ("authorization", "Authorization", "COOKIE", "token", "Secret", "password", "apiKey", "api_key"):
            with self.assertRaises(SecretMaterialError):
                assert_no_secret_material({key: "value"})

    def test_assert_no_secret_material_raises_on_composite_secret_keys(self) -> None:
        for key in ("sessionToken", "refreshToken", "auth_token", "x-api-key", "clientSecret", "dbPassword", "awsCredentials"):
            with self.assertRaises(SecretMaterialError):
                assert_no_secret_material({key: "value"})

    def test_assert_no_secret_material_allows_plural_token_counters(self) -> None:
        assert_no_secret_material(
            {
                "usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 2},
                "modelUsage": {"grok-4.5-build": {"input_tokens": 10, "reasoning_tokens": 3}},
                "tool": "tokenizer",
            }
        )

    def test_assert_no_secret_material_raises_on_plural_secret_token_keys(self) -> None:
        # Round5 plural-token-key-bypass: plural "*Tokens" keys holding arrays of
        # real token strings are secret-shaped too (not just the singular form).
        for key in ("accessTokens", "refreshTokens", "sessionTokens", "authTokens", "apiKeys", "access_tokens"):
            with self.subTest(key=key):
                with self.assertRaises(SecretMaterialError):
                    assert_no_secret_material({key: ["opaque-secret-1", "opaque-secret-2"]})

    def test_assert_no_secret_material_raises_on_interior_token_segment_keys(self) -> None:
        # round6 secret-key-name-suffix: a "token" WORD SEGMENT anywhere in the key
        # (not only as the trailing suffix) is secret-shaped, so a key like
        # refreshTokenValue -- holding a real opaque OAuth refresh token -- is
        # caught even though it ends in "value", not "token".
        for key in ("refreshTokenValue", "session_token_string", "tokenSecretPair", "access-token-blob"):
            with self.subTest(key=key):
                with self.assertRaises(SecretMaterialError):
                    assert_no_secret_material(
                        {"upstream": {key: "1//0gABCDEfghijKLMNOpqrstuVWXYZ0123456789abcdef-opaque"}}
                    )

    def test_assert_no_secret_material_still_allows_interior_token_count_fields(self) -> None:
        # The interior-segment rule must NOT redact the benign token-COUNT usage
        # fields, which carry integer counters, not credentials.
        assert_no_secret_material(
            {"usage": {"cacheReadInputTokens": 2, "acceptedPredictionTokens": 4, "reasoning_tokens": 1}}
        )

    def test_assert_no_secret_material_raises_on_acronym_glued_token_keys(self) -> None:
        # Round7 F2 acronym-glued-key-name: a "Token" word glued directly onto an
        # all-caps acronym (IDTokenHint, JWTTokenPayload, SSOTokenBlob) was missed --
        # the camelCase splitter never fired between two uppercase letters, so the
        # interior "Token" was glued into the acronym and evaded both the segment
        # check and the trailing-suffix fallback. The acronym-run boundary now splits
        # it out, so an opaque secret under such a key is caught.
        for key in ("IDTokenHint", "JWTTokenPayload", "SSOTokenBlob", "APITokenValue"):
            with self.subTest(key=key):
                with self.assertRaises(SecretMaterialError):
                    assert_no_secret_material(
                        {"detail": {key: "1//0gABCDEfghijKLMNOpqrstuVWXYZ0123456789abcdef-opaque"}}
                    )

    def test_assert_no_secret_material_still_allows_acronym_prefixed_token_counters(self) -> None:
        # The acronym-boundary split must NOT start redacting benign token-COUNT
        # fields: their normalized "*tokens" suffix still excludes them.
        assert_no_secret_material(
            {"usage": {"llmInputTokens": 7, "gpuOutputTokens": 3, "cacheReadInputTokens": 1}}
        )

    def test_failure_envelope_redacts_secret_shaped_detail_key_without_dropping_envelope(self) -> None:
        # round6 failure-detail-key-redaction: a detail whose key merely CONTAINS a
        # secret-shaped substring (a benign "credentialDelegation" flag) must NOT
        # make failure_envelope raise SecretMaterialError and lose the classified
        # failure; it returns the correctly-classified envelope with the key redacted.
        env = failure_envelope(
            run_id=_RUN_ID,
            mode="preflight",
            error_class="sandbox-failure",
            message="sandbox profile misconfigured",
            detail={"credentialDelegation": True, "reason": "sandbox profile requires it"},
        )
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "sandbox-failure")
        # The non-secret sibling survives; the secret-shaped key is gone.
        self.assertEqual(env["error"]["detail"]["reason"], "sandbox profile requires it")
        self.assertNotIn("credentialDelegation", json.dumps(env))
        assert_no_secret_material(env)

    def test_failure_envelope_suppresses_opaque_secret_under_secret_shaped_key(self) -> None:
        # A real opaque credential held under a secret-shaped key (one the value
        # patterns would NOT recognize) is suppressed, not leaked, on the failure
        # envelope -- consistently with what the scanner flags.
        env = failure_envelope(
            run_id=_RUN_ID,
            mode="review",
            error_class="cli-failure",
            message="upstream refresh failed",
            detail={"refreshTokenValue": "1//0gOPAQUErefreshtokenNOTpatternshaped0123456789abcdef"},
        )
        self.assertNotIn("1//0gOPAQUErefreshtoken", json.dumps(env))
        assert_no_secret_material(env)

    def test_assert_no_secret_material_raises_on_github_fine_grained_pat(self) -> None:
        # Round5 github-fine-grained-pat-missing: the currently-recommended
        # github_pat_ prefix must be flagged (and, below, redacted).
        pat = _GITHUB_PAT
        with self.assertRaises(SecretMaterialError):
            assert_no_secret_material({"error": {"detail": {"stderr": pat}}})

    def test_redact_removes_github_fine_grained_pat_value(self) -> None:
        pat = _GITHUB_PAT
        cleaned = redact_secret_value_text("token {} in log".format(pat))
        self.assertNotIn(pat, cleaned)
        self.assertIn("[redacted-github-token]", cleaned)

    def test_redact_removes_pem_body_without_end_marker(self) -> None:
        # Round5 pem-truncated-body-leak (a): a PEM block whose END line was cut
        # off (truncation / chunk boundary) must still have its base64 body removed
        # to end-of-string, never left verbatim, and the result must pass the scanner.
        body = "MIIBVeryLongBase64KeyMaterial0123456789ABCDEFabcdef"
        raw = _PEM_RSA_BEGIN + "\n" + body + "\nmore-body-9876543210"
        cleaned = redact_secret_value_text(raw)
        self.assertNotIn(body, cleaned)
        self.assertNotIn("more-body-9876543210", cleaned)
        self.assertIn("[redacted-pem-private-key]", cleaned)
        assert_no_secret_material({"note": cleaned})

    def test_redact_removes_multiline_pem_block_whole(self) -> None:
        body = "MIIBkeymaterial1234567890ABCDEFabcdef0987654321"
        raw = _PEM_RSA_BEGIN + "\n" + body + "\n" + _PEM_RSA_END
        cleaned = redact_secret_value_text(raw)
        self.assertNotIn(body, cleaned)
        assert_no_secret_material({"note": cleaned})

    def test_redact_secret_text_stream_catches_split_bearer(self) -> None:
        # Round5 / Grok dogfood-3 #6: a bearer token split across two ~480-char
        # stream chunks matches no single chunk but the concatenation IS the secret.
        from groklib.envelope import redact_secret_text_stream

        first = "some preface text bearer eyJab"
        second = "cdef0123456789ABCDEFghij trailing"
        redacted = redact_secret_text_stream([first, second])
        self.assertNotIn("cdef0123456789ABCDEFghij", "".join(redacted))
        assert_no_secret_material({"events": [{"data": {"text": t}} for t in redacted]})

    def test_redact_secret_text_stream_catches_split_pem(self) -> None:
        from groklib.envelope import redact_secret_text_stream

        first = _PEM_RSA_BEGIN + "\nMIIBkeymaterial"
        second = "0123456789ABCDEF\n" + _PEM_RSA_END
        redacted = redact_secret_text_stream([first, second])
        self.assertNotIn("MIIBkeymaterial", "".join(redacted))
        assert_no_secret_material({"events": [{"data": {"text": t}} for t in redacted]})

    def test_assert_no_secret_material_raises_on_bearer_value(self) -> None:
        with self.assertRaises(SecretMaterialError):
            assert_no_secret_material(
                {"detail": "Authorization header sent: Bearer " + _BEARER_SHORT}
            )

    def test_assert_no_secret_material_raises_on_raw_token_shapes(self) -> None:
        # SEC2: raw credential VALUES leaking under a benign key (e.g. a token in
        # error.detail.stderr) fail closed on their shape, not just on "bearer".
        raw_tokens = (
            _XAI_LONG,
            _SK_LEGACY,
            _JWT,
        )
        for token in raw_tokens:
            with self.subTest(token=token[:8]):
                with self.assertRaises(SecretMaterialError):
                    assert_no_secret_material({"error": {"detail": {"stderr": token}}})

    def test_assert_no_secret_material_raises_on_tuple_nested_secret(self) -> None:
        # round3 envelope-tuple-secret-scan-gap: json.dumps serializes a tuple as
        # a JSON array, so a secret nested inside a tuple reaches stdout/disk just
        # like one nested inside a list and MUST be scanned, not fall through.
        with self.assertRaises(SecretMaterialError):
            assert_no_secret_material(
                {
                    "error": {
                        "detail": {
                            "stderrTail": (
                                "retrying request",
                                "Authorization: Bearer " + _fx("abcdef123456", "ABCDEF"),
                            )
                        }
                    }
                }
            )
        # A secret-shaped KEY inside a dict nested in a tuple is caught too.
        with self.assertRaises(SecretMaterialError):
            assert_no_secret_material({"items": ({"safe": 1}, {"token": "xyz"})})

    def test_assert_no_secret_material_raises_on_extended_token_shapes(self) -> None:
        # round3: the scanner must also catch AWS access-key ids, GitHub tokens,
        # Slack tokens, and PEM private-key blocks under any benign key.
        # Split AWS/GitHub/Slack shapes so source never holds a contiguous secret
        # literal (GitHub secret scanning flags AWS docs EXAMPLE keys as Temporary
        # Access Key IDs).
        raw_tokens = (
            _AWS_AKIA,
            _GHP,
            _XOXB,
            _PEM_RSA_BEGIN + "\nMIIBkeymaterial1234567890\n" + _PEM_RSA_END,
        )
        for token in raw_tokens:
            with self.subTest(token=token[:10]):
                with self.assertRaises(SecretMaterialError):
                    assert_no_secret_material({"error": {"detail": {"stderr": token}}})

    def test_assert_no_secret_material_raises_on_pure_alpha_bearer(self) -> None:
        # round3 bearer-pattern-pure-alpha-token-false-negative: an all-letter
        # bearer credential (no digit/symbol) must still be flagged.
        with self.assertRaises(SecretMaterialError):
            assert_no_secret_material({"detail": "Authorization: Bearer " + _BEARER_ALPHA})

    def test_assert_no_secret_material_allows_ordinary_text_and_counters(self) -> None:
        # SEC2 false-positive guard: prose containing the "sk-" fragment and
        # ordinary usage counters must NOT trip the raw-token value patterns.
        assert_no_secret_material(
            {
                "warnings": ["task-force review", "risk-averse plan", "asked the operator"],
                "usage": {"inputTokens": 10, "cacheReadInputTokens": 2},
                "note": "the pkg builds cleanly",
            }
        )

    def test_assert_no_secret_material_raises_on_nested_secret(self) -> None:
        with self.assertRaises(SecretMaterialError):
            assert_no_secret_material({"outer": {"deeply": {"nested": {"token": "xyz"}}}})

        with self.assertRaises(SecretMaterialError):
            assert_no_secret_material({"list": [{"safe": True}, {"secret": "xyz"}]})

    def test_assert_no_secret_material_is_a_grok_wrapper_error(self) -> None:
        with self.assertRaises(GrokWrapperError):
            assert_no_secret_material({"token": "xyz"})

    def test_secret_scan_guard(self) -> None:
        # round6 failure-detail-key-redaction: a secret-shaped KEY in the caller's
        # detail is renamed-and-suppressed (the same treatment a secret VALUE
        # already gets), so the CLASSIFIED failure envelope is preserved rather
        # than being dropped by build_envelope's fail-closed key scanner -- while
        # the secret value still never reaches stdout.
        env_key = failure_envelope(
            run_id=_RUN_ID,
            mode="review",
            error_class="cli-failure",
            message="grok exited nonzero",
            detail={"authorization": "should never appear"},
        )
        self.assertEqual(env_key["status"], "failure")
        self.assertEqual(env_key["error"]["class"], "cli-failure")
        self.assertNotIn("should never appear", json.dumps(env_key))
        self.assertNotIn("authorization", json.dumps(env_key))
        assert_no_secret_material(env_key)

        # Grok dogfood-3 #5: a KNOWN secret shape in error.detail is now
        # redacted-and-reported (the same treatment response/stderr already get),
        # not fail-closed-dropped: failure_envelope masks it before build_envelope,
        # so the operator still gets the (redacted) diagnostic on stdout.
        env = failure_envelope(
            run_id=_RUN_ID,
            mode="review",
            error_class="cli-failure",
            message="grok exited nonzero: Bearer " + _fx("abc123", "def456"),
            detail={"note": "sent header Bearer " + _fx("abc123", "def456")},
        )
        self.assertNotIn(_fx("abc123", "def456"), json.dumps(env))
        self.assertIn("[redacted-bearer-token]", env["error"]["message"])
        self.assertIn("[redacted-bearer-token]", env["error"]["detail"]["note"])

class FailureEnvelopeDetailRedactionTests(unittest.TestCase):
    """Grok dogfood-3 #5: error.detail + message are systematically redacted before build."""

    def test_worktree_git_style_stderr_detail_is_redacted(self) -> None:
        # worktree._git embeds raw git stderr into detail={"argv":..., "stderr":...};
        # a secret quoted there (a path/hook/config value) must be masked, not ride
        # to stdout, and must not fail-closed-drop the whole diagnostic.
        detail = {
            "argv": ["git", "-C", "/repo", "push"],
            "exitStatus": 128,
            "stderr": "remote rejected: token {} leaked in hook".format(_SK_LEGACY_LOWER),
        }
        env = failure_envelope(
            run_id=_RUN_ID,
            mode="code",
            error_class="worktree-failure",
            message="git command failed",
            detail=detail,
        )
        self.assertNotIn(_SK_LEGACY_LOWER, json.dumps(env))
        self.assertIn("[redacted-api-key-token]", env["error"]["detail"]["stderr"])
        # Non-secret detail fields survive verbatim.
        self.assertEqual(env["error"]["detail"]["exitStatus"], 128)

class RedactSecretMaterialTests(unittest.TestCase):
    """redact_secret_material neutralizes exactly what assert_no_secret_material flags (F-STATUS-SECRET)."""

    def test_redacted_bearer_jwt_and_api_key_pass_the_scanner(self) -> None:
        raw = (
            "Authorization: Bearer "
            + _JWT_SHORT
            + " "
            + "with api key "
            + _SK_LEGACY_LOWER
            + " inline"
        )
        redacted = redact_secret_material({"data": {"text": raw}, "list": [raw]})
        # The redacted structure passes the scanner (would previously raise).
        assert_no_secret_material(redacted)
        cleaned = redacted["data"]["text"]
        self.assertNotIn("Bearer ", cleaned)
        self.assertNotIn(_SK_LEGACY_LOWER, cleaned)
        self.assertNotIn(_JWT_SHORT, cleaned)
        self.assertIn("[redacted-", cleaned)

    def test_redaction_preserves_benign_content_and_structure(self) -> None:
        payload = {"count": 3, "flag": True, "items": ["CLAUDE.md", "reviewing the diff"], "nothing": None}
        self.assertEqual(redact_secret_material(payload), payload)

    def test_redaction_does_not_touch_keys(self) -> None:
        # Keys are left untouched: the helper only masks string VALUES.
        self.assertEqual(redact_secret_material({"bearer note": "clean"}), {"bearer note": "clean"})

    def test_redaction_removes_the_opaque_bearer_credential_value(self) -> None:
        # Round-2 regression (bearer-redaction-leaves-secret-intact): an opaque
        # (non-JWT, non-sk/xai) bearer token must have its CREDENTIAL VALUE
        # removed, not just its "bearer" label -- assert the value substring is
        # gone, and the redacted string then passes the fail-closed scanner.
        secret_value = _BEARER_OPAQUE
        raw = "Authorization: Bearer {}".format(secret_value)
        cleaned = redact_secret_value_text(raw)
        self.assertNotIn(secret_value, cleaned)
        self.assertNotIn("Bearer ", cleaned)
        self.assertIn("[redacted-bearer-token]", cleaned)
        assert_no_secret_material({"detail": cleaned})

    def test_redaction_removes_each_pattern_value_substring(self) -> None:
        # The redacted output must not contain any of the source secret VALUES,
        # for every pattern (bearer+value, sk-, xai-, JWT).
        cases = (
            "Bearer " + _BEARER_OPAQUE,
            _SK_LEGACY_LOWER,
            _XAI_LONG,
            _JWT_SHORT,
        )
        for secret in cases:
            with self.subTest(secret=secret[:10]):
                cleaned = redact_secret_value_text("prefix {} suffix".format(secret))
                # The full secret body (minus a bare "Bearer " prefix) is gone.
                body = secret[len("Bearer ") :] if secret.startswith("Bearer ") else secret
                self.assertNotIn(body, cleaned)
                self.assertIn("[redacted-", cleaned)
                assert_no_secret_material({"detail": cleaned})

    def test_redaction_removes_extended_pattern_values(self) -> None:
        # round3: AWS/GitHub/Slack credential VALUES must be removed, and the
        # redacted string then passes the fail-closed scanner.
        cases = (
            ("aws", _AWS_AKIA),
            ("github", _GHP),
            ("slack", _XOXB),
        )
        for label, secret in cases:
            with self.subTest(label=label):
                cleaned = redact_secret_value_text("leaked {} value".format(secret))
                self.assertNotIn(secret, cleaned)
                self.assertIn("[redacted-", cleaned)
                assert_no_secret_material({"detail": cleaned})

    def test_redaction_removes_pem_private_key_block(self) -> None:
        # round3: the whole PEM block (key material included) must be removed,
        # not just the -----BEGIN----- header line.
        pem = (
            _PEM_RSA_BEGIN
            + "\n"
            + "MIIBOgIBAAJBAKtopsecretkeymaterialdonotleak1234567890\n"
            + _PEM_RSA_END
        )
        cleaned = redact_secret_value_text("key: {} done".format(pem))
        self.assertNotIn("topsecretkeymaterial", cleaned)
        self.assertNotIn(_PEM_RSA_BEGIN, cleaned)
        self.assertIn("[redacted-pem-private-key]", cleaned)
        assert_no_secret_material({"detail": cleaned})

    def test_redaction_removes_tuple_nested_secret_value(self) -> None:
        # round3: redact_secret_material recurses into tuples (via the shared
        # tree-walker), so a secret nested in a tuple is masked; the tuple is
        # rebuilt as a JSON-shaped list and the source value is gone entirely.
        secret = "Authorization: Bearer " + _BEARER_MIXED
        redacted = redact_secret_material({"stderrTail": ("retry", secret)})
        assert_no_secret_material(redacted)
        self.assertNotIn(_BEARER_MIXED, json.dumps(redacted))

    def test_redaction_removes_pure_alpha_bearer_value(self) -> None:
        # round3 bearer-pattern-pure-alpha-token-false-negative: the all-letter
        # credential value is removed, and the redacted string passes the scanner.
        secret = _BEARER_ALPHA
        cleaned = redact_secret_value_text("Authorization: Bearer {}".format(secret))
        self.assertNotIn(secret, cleaned)
        self.assertIn("[redacted-bearer-token]", cleaned)
        assert_no_secret_material({"detail": cleaned})

    def test_redaction_leaves_benign_bearer_prose_intact(self) -> None:
        # bearer-pattern-over-redacts-benign-prose: ordinary prose mentioning the
        # word "bearer" (no real credential following) must NOT be mangled.
        for prose in (
            "the bearer of good news said hello",
            "the bearer of this token must present valid credentials",
            "Bearer authentication credentials are required",
        ):
            with self.subTest(prose=prose):
                self.assertEqual(redact_secret_value_text(prose), prose)

class StructuralViolationValueLeakTests(unittest.TestCase):
    """Round4 F1: a structural violation must never embed the raw offending VALUE."""

    _SECRET = "Bearer " + _XAI_LEAK

    def test_enum_violation_does_not_leak_secret_value(self) -> None:
        # build_envelope logs the violation to stderr AND embeds it in the raised
        # InvalidEnvelopeError.detail BEFORE assert_no_secret_material runs, so the
        # secret-shaped enum value must be redacted, never rendered verbatim.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(InvalidEnvelopeError) as ctx:
                build_envelope(run_id=_RUN_ID, mode="review", status=self._SECRET)
        violations = ctx.exception.detail.get("violations")
        joined = " ".join(violations) if isinstance(violations, list) else ""
        self.assertNotIn(_XAI_LEAK, joined)
        self.assertNotIn(_XAI_LEAK, str(ctx.exception))
        self.assertNotIn(_XAI_LEAK, stderr.getvalue())
        # The redacted placeholder still communicates that a value was present.
        self.assertIn("[redacted-", joined)

    def test_failure_envelope_error_class_precheck_does_not_leak_secret(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(InvalidEnvelopeError) as ctx:
                failure_envelope(run_id=_RUN_ID, mode="review", error_class=self._SECRET, message="x")
        self.assertNotIn(_XAI_LEAK, str(ctx.exception))
        self.assertNotIn(_XAI_LEAK, str(ctx.exception.detail))
        self.assertNotIn(_XAI_LEAK, stderr.getvalue())

class ApiKeyPatternTighteningTests(unittest.TestCase):
    """Round4 F2: the api-key pattern must not over-match ordinary kebab identifiers."""

    def test_benign_kebab_identifiers_are_not_redacted(self) -> None:
        for benign in (
            "packages/sk-learn-preprocessing-utils.py",
            "feature/sk-image-augmentation-pipeline",
            "xai-model-training-harness-notes",
        ):
            with self.subTest(benign=benign):
                self.assertEqual(redact_secret_value_text(benign), benign)
                # And a benign identifier must NOT abort a real run.
                assert_no_secret_material({"changedFiles": [benign]})

    def test_real_api_keys_still_redacted_and_flagged(self) -> None:
        for key in (
            _XAI_LONG,
            _SK_LEGACY,
        ):
            with self.subTest(key=key):
                cleaned = redact_secret_value_text("token {} here".format(key))
                self.assertNotIn(key, cleaned)
                self.assertIn("[redacted-api-key-token]", cleaned)
                with self.assertRaises(SecretMaterialError):
                    assert_no_secret_material({"detail": {"stderr": key}})

    def test_current_provider_key_formats_are_redacted(self) -> None:
        # round6 api-key-under-match: the round5 no-hyphen body missed the current
        # OpenAI project ("sk-proj-...") and Anthropic ("sk-ant-api03-...") formats
        # entirely because the hyphen 3-5 chars in broke the {20,} run. All four
        # current provider shapes MUST be caught (value absent), NOT just legacy.
        for key in (
            _SK_PROJ,
            _SK_ANT_API,
            _SK_ANT_ADMIN,
            _SK_LEGACY,
            _XAI_LONG,
        ):
            with self.subTest(key=key[:14]):
                cleaned = redact_secret_value_text("leaked key {} in output".format(key))
                self.assertNotIn(key, cleaned)
                self.assertIn("[redacted-api-key-token]", cleaned)
                with self.assertRaises(SecretMaterialError):
                    assert_no_secret_material({"error": {"detail": {"stderr": key}}})

    def test_kebab_and_branch_identifiers_are_not_redacted(self) -> None:
        # The provider-format widening must NOT re-introduce kebab over-match: a
        # dictionary-word kebab id or a git branch name (even one with a digit)
        # must survive verbatim and must never abort a run.
        for benign in (
            "sk-learn-preprocessing-utils",
            "feature/sk-image-augmentation-pipeline",
            "sk-proj-cost-model-v2-plan",
            "chore/sk-ant-migration-notes",
        ):
            with self.subTest(benign=benign):
                self.assertEqual(redact_secret_value_text(benign), benign)
                assert_no_secret_material({"changedFiles": [benign]})

    def test_aws_temporary_asia_credentials_are_redacted(self) -> None:
        # round6 aws-ASIA: STS temporary/session creds use the ASIA prefix (the
        # default in CI/CD, Lambda, assumed-role) and MUST be flagged alongside AKIA.
        # Concatenate so the full AKIA/ASIA id never appears as one source literal.
        for key in (_AWS_ASIA, _AWS_AKIA):
            with self.subTest(key=key):
                cleaned = redact_secret_value_text("AWS_ACCESS_KEY_ID={}".format(key))
                self.assertNotIn(key, cleaned)
                self.assertIn("[redacted-aws-access-key-id]", cleaned)
                with self.assertRaises(SecretMaterialError):
                    assert_no_secret_material({"note": key})

class PgpPrivateKeyPatternTests(unittest.TestCase):
    """Round4 F5: the PEM pattern must also catch the PGP armored private-key header."""

    _PGP = (
        _PEM_PGP_BEGIN
        + "\n"
        + "lQVYBGXtopsecretpgpkeymaterialdonotleak1234567890\n"
        + _PEM_PGP_END
    )

    def test_pgp_private_key_block_is_flagged(self) -> None:
        with self.assertRaises(SecretMaterialError):
            assert_no_secret_material({"error": {"detail": {"stderr": self._PGP}}})

    def test_pgp_private_key_block_is_redacted(self) -> None:
        cleaned = redact_secret_value_text("key: {} done".format(self._PGP))
        self.assertNotIn("topsecretpgpkeymaterial", cleaned)
        self.assertIn("[redacted-pem-private-key]", cleaned)
        assert_no_secret_material({"detail": cleaned})
