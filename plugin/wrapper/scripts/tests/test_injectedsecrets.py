# wrapper/scripts/tests/test_injectedsecrets.py

import json
import os
import pathlib
import shutil
import tempfile
import unittest

from groklib import authhome, injectedsecrets
from groklib.envelope import build_envelope
from groklib.injectedsecrets import (
    INJECTED_CREDENTIAL_PLACEHOLDER,
    clear_injected_secret_denylist,
    current_injected_secret_denylist,
    extract_injected_secret_values,
    redact_injected_secrets,
    set_injected_secret_denylist,
)

from tests.temphomeisolation import TempHomeIsolationMixin

_RUN_ID = "20260715T090000Z-abcdef"

# A 40-char opaque token that matches NONE of envelope._SECRET_VALUE_PATTERNS
# (no bearer/xai-/sk-/eyJ/AKIA/gh_/xox/PEM shape): only lowercase letters and
# digits with no special prefix. Pattern redaction cannot catch this; only the
# D4(a) exact-value denylist can.
_OPAQUE_TOKEN_40 = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0"


class InjectedSecretDenylistTests(unittest.TestCase):
    """Covers the D4(a) per-run denylist registry and exact-value redactor."""

    def setUp(self) -> None:
        # The denylist is a per-run process global; guarantee no test leaks it.
        self.addCleanup(clear_injected_secret_denylist)
        clear_injected_secret_denylist()

    def test_set_dedupes_drops_empty_and_sorts_by_descending_length(self) -> None:
        set_injected_secret_denylist(["short-secret-16c", "short-secret-16c", "", _OPAQUE_TOKEN_40])
        denylist = current_injected_secret_denylist()
        self.assertEqual(denylist, (_OPAQUE_TOKEN_40, "short-secret-16c"))

    def test_clear_empties_the_denylist(self) -> None:
        set_injected_secret_denylist([_OPAQUE_TOKEN_40])
        self.assertTrue(current_injected_secret_denylist())
        clear_injected_secret_denylist()
        self.assertEqual(current_injected_secret_denylist(), ())

    def test_redact_masks_exact_value_in_string_leaf(self) -> None:
        set_injected_secret_denylist([_OPAQUE_TOKEN_40])
        masked = redact_injected_secrets("the token is {} echoed".format(_OPAQUE_TOKEN_40))
        self.assertEqual(masked, "the token is {} echoed".format(INJECTED_CREDENTIAL_PLACEHOLDER))
        self.assertNotIn(_OPAQUE_TOKEN_40, masked)

    def test_redact_masks_exact_value_used_as_dict_key(self) -> None:
        set_injected_secret_denylist([_OPAQUE_TOKEN_40])
        masked = redact_injected_secrets({_OPAQUE_TOKEN_40: "value"})
        self.assertEqual(masked, {INJECTED_CREDENTIAL_PLACEHOLDER: "value"})

    def test_redact_recurses_into_lists_and_nested_dicts(self) -> None:
        set_injected_secret_denylist([_OPAQUE_TOKEN_40])
        payload = {"outer": [{"inner": "prefix-{}".format(_OPAQUE_TOKEN_40)}]}
        masked = redact_injected_secrets(payload)
        self.assertEqual(masked, {"outer": [{"inner": "prefix-{}".format(INJECTED_CREDENTIAL_PLACEHOLDER)}]})

    def test_redact_is_noop_when_denylist_empty(self) -> None:
        benign = {"text": "this is a perfectly benign response string"}
        self.assertIs(redact_injected_secrets(benign), benign)

    def test_redact_leaves_benign_string_untouched_when_denylist_set(self) -> None:
        set_injected_secret_denylist([_OPAQUE_TOKEN_40])
        benign = "a perfectly benign response with no injected credential"
        self.assertEqual(redact_injected_secrets(benign), benign)


class BuildEnvelopeInjectedRedactionTests(unittest.TestCase):
    """The D4(a) redaction must run inside build_envelope, before stdout emission."""

    def setUp(self) -> None:
        self.addCleanup(clear_injected_secret_denylist)
        clear_injected_secret_denylist()

    def test_build_envelope_redacts_exact_injected_token_in_response_text(self) -> None:
        set_injected_secret_denylist([_OPAQUE_TOKEN_40])
        response = {
            "text": "grok echoed its own token: {}".format(_OPAQUE_TOKEN_40),
            "structured": None,
            "stopReason": "end",
        }
        envelope = build_envelope(run_id=_RUN_ID, mode="code", status="success", response=response)
        serialized = json.dumps(envelope, sort_keys=True)
        self.assertNotIn(_OPAQUE_TOKEN_40, serialized)
        self.assertIn(INJECTED_CREDENTIAL_PLACEHOLDER, envelope["response"]["text"])

    def test_build_envelope_redacts_injected_token_used_as_structured_key(self) -> None:
        set_injected_secret_denylist([_OPAQUE_TOKEN_40])
        response = {"text": None, "structured": {_OPAQUE_TOKEN_40: "leaked"}, "stopReason": "end"}
        envelope = build_envelope(run_id=_RUN_ID, mode="code", status="success", response=response)
        serialized = json.dumps(envelope, sort_keys=True)
        self.assertNotIn(_OPAQUE_TOKEN_40, serialized)
        self.assertIn(INJECTED_CREDENTIAL_PLACEHOLDER, envelope["response"]["structured"])

    def test_build_envelope_leaves_benign_response_untouched(self) -> None:
        set_injected_secret_denylist([_OPAQUE_TOKEN_40])
        response = {"text": "a benign answer with no credential in it", "structured": None, "stopReason": "end"}
        envelope = build_envelope(run_id=_RUN_ID, mode="code", status="success", response=response)
        self.assertEqual(envelope["response"]["text"], "a benign answer with no credential in it")
        self.assertNotIn(INJECTED_CREDENTIAL_PLACEHOLDER, json.dumps(envelope))


class ExtractInjectedSecretsTests(unittest.TestCase):
    """Covers extraction of the denylist from a copied auth.json (fail-safe)."""

    def setUp(self) -> None:
        self.scratch_dir = tempfile.mkdtemp(prefix="grok-cli-injected-test-")
        self.addCleanup(shutil.rmtree, self.scratch_dir, True)
        self.grok_dir = pathlib.Path(self.scratch_dir) / ".grok"
        self.grok_dir.mkdir()

    def _write_auth(self, content: bytes) -> None:
        (self.grok_dir / "auth.json").write_bytes(content)

    def test_extracts_string_leaves_at_or_above_threshold_and_ignores_short_and_keys(self) -> None:
        self._write_auth(
            json.dumps(
                {
                    "accessToken": _OPAQUE_TOKEN_40,
                    "short": "tiny",
                    "nested": {"refreshTokenValue": "another-long-credential-32chars-x"},
                    "list": ["yet-another-long-credential-value"],
                }
            ).encode("utf-8")
        )
        values = extract_injected_secret_values(self.grok_dir, ("auth.json",))
        self.assertIn(_OPAQUE_TOKEN_40, values)
        self.assertIn("another-long-credential-32chars-x", values)
        self.assertIn("yet-another-long-credential-value", values)
        self.assertNotIn("tiny", values)
        # Key names are field labels, never credentials.
        self.assertNotIn("accessToken", values)

    def test_malformed_auth_json_degrades_to_empty_without_raising(self) -> None:
        self._write_auth(b"this is not valid json at all")
        values = extract_injected_secret_values(self.grok_dir, ("auth.json",))
        self.assertEqual(values, ())

    def test_missing_auth_file_degrades_to_empty_without_raising(self) -> None:
        values = extract_injected_secret_values(self.grok_dir, ("auth.json",))
        self.assertEqual(values, ())

    def test_register_from_home_sets_denylist_and_is_reset_on_next_call(self) -> None:
        self.addCleanup(clear_injected_secret_denylist)
        self._write_auth(json.dumps({"token": _OPAQUE_TOKEN_40}).encode("utf-8"))
        injectedsecrets.register_injected_secrets_from_home(self.grok_dir, ("auth.json",))
        self.assertEqual(current_injected_secret_denylist(), (_OPAQUE_TOKEN_40,))
        # Trustworthy new home values replace (never accumulate stale prior home).
        other = "z9y8x7w6v5u4t3s2r1q0" + "p9o8n7m6l5k4j3i2h1g0"
        self._write_auth(json.dumps({"token": other}).encode("utf-8"))
        injectedsecrets.register_injected_secrets_from_home(self.grok_dir, ("auth.json",))
        self.assertEqual(current_injected_secret_denylist(), (other,))

    def test_register_preserves_existing_denylist_when_extraction_empty(self) -> None:
        """Missing/malformed home must not wipe a valid non-empty in-memory denylist."""
        self.addCleanup(clear_injected_secret_denylist)
        set_injected_secret_denylist([_OPAQUE_TOKEN_40])
        self.assertEqual(current_injected_secret_denylist(), (_OPAQUE_TOKEN_40,))
        # Malformed auth: extract empty, preserve existing.
        self._write_auth(b"broken")
        injectedsecrets.register_injected_secrets_from_home(self.grok_dir, ("auth.json",))
        self.assertEqual(current_injected_secret_denylist(), (_OPAQUE_TOKEN_40,))
        # Missing auth file: still preserve.
        (self.grok_dir / "auth.json").unlink()
        injectedsecrets.register_injected_secrets_from_home(self.grok_dir, ("auth.json",))
        self.assertEqual(current_injected_secret_denylist(), (_OPAQUE_TOKEN_40,))
        # Empty process + missing home stays pattern-only (empty).
        clear_injected_secret_denylist()
        injectedsecrets.register_injected_secrets_from_home(self.grok_dir, ("auth.json",))
        self.assertEqual(current_injected_secret_denylist(), ())


class CreatePrivateHomeRegistersInjectedSecretsTests(TempHomeIsolationMixin, unittest.TestCase):
    """create_private_home must register the injected denylist, fail-safe on malformed auth."""

    def setUp(self) -> None:
        super().setUp()
        self.scratch_dir = tempfile.mkdtemp(prefix="grok-cli-injected-home-test-")
        self.addCleanup(shutil.rmtree, self.scratch_dir, True)
        self.addCleanup(clear_injected_secret_denylist)
        clear_injected_secret_denylist()
        self.source_grok_dir = pathlib.Path(self.scratch_dir) / "source-grok"
        self.source_grok_dir.mkdir()

    def _write_source_auth(self, content: bytes) -> None:
        path = self.source_grok_dir / "auth.json"
        path.write_bytes(content)
        os.chmod(path, 0o600)

    def _cleanup_home(self, home: authhome.PrivateHome) -> None:
        if home.home_dir.exists():
            shutil.rmtree(str(home.home_dir), ignore_errors=True)

    def test_valid_auth_json_registers_injected_token(self) -> None:
        self._write_source_auth(json.dumps({"access_token": _OPAQUE_TOKEN_40}).encode("utf-8"))
        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
        )
        self.addCleanup(self._cleanup_home, home)
        self.assertEqual(current_injected_secret_denylist(), (_OPAQUE_TOKEN_40,))
        # PrivateHome must NOT carry any auth-derived value (secrets discipline).
        self.assertNotIn(_OPAQUE_TOKEN_40, repr(home))

    def test_malformed_auth_json_leaves_denylist_empty_without_crashing(self) -> None:
        self._write_source_auth(b"AUTH-FILE-CONTENT-NOT-JSON")
        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
        )
        self.addCleanup(self._cleanup_home, home)
        self.assertEqual(current_injected_secret_denylist(), ())


if __name__ == "__main__":
    unittest.main()
