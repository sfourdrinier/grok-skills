# wrapper/scripts/tests/test_acp.py
#
# ACP ndjson JSON-RPC 2.0 client framing (Task 5.3). RED first against a fake
# stdio peer subprocess that echoes canned JSON-RPC responses.

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

from groklib import GrokWrapperError


_FAKE_PEER = textwrap.dedent(
    r"""
    import json
    import sys

    def read_msg():
        line = sys.stdin.readline()
        if not line:
            return None
        return json.loads(line)

    def write_msg(obj):
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    while True:
        msg = read_msg()
        if msg is None:
            break
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            # Echo clientInfo so tests can assert the honest plugin version id
            # the wrapper advertises (stable packaging version, not "experimental").
            params = msg.get("params") or {}
            write_msg({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {"embeddedContext": True},
                        "mcpCapabilities": {"http": True, "sse": True},
                    },
                    "echoedClientInfo": params.get("clientInfo"),
                    "_meta": {
                        "x.ai/hooks": {
                            "blockingEvents": ["pre_tool_use"],
                            "decisions": ["deny"],
                        }
                    },
                },
            })
        elif method == "session/new":
            write_msg({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"sessionId": "sess-fake-1", "model": "grok-4.5"},
            })
        elif method == "session/prompt":
            write_msg({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sess-fake-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello "},
                    },
                },
            })
            write_msg({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sess-fake-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "world"},
                    },
                },
            })
            write_msg({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"stopReason": "end_turn", "usage": {"inputTokens": 1}},
            })
        elif method == "session/cancel":
            write_msg({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            write_msg({
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": "unknown method"},
            })
    """
).strip() + "\n"


class AcpFramingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="grok-acp-test-")
        self.peer_script = pathlib.Path(self._tmpdir) / "fake_acp_peer.py"
        self.peer_script.write_text(_FAKE_PEER, encoding="utf-8")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_encode_decode_roundtrip(self) -> None:
        from groklib import acp

        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        frame = acp.encode_frame(payload)
        self.assertTrue(frame.endswith(b"\n"))
        decoded = acp.decode_frame(frame)
        self.assertEqual(decoded, payload)

    def test_decode_frame_rejects_invalid_utf8(self) -> None:
        from groklib import acp
        from groklib import GrokWrapperError

        # Invalid UTF-8 (lone continuation byte) must fail closed, not be
        # silently replaced with U+FFFD and parsed (review: fail-closed stream).
        with self.assertRaises(GrokWrapperError) as ctx:
            acp.decode_frame(b'{"a":"\xff\xfe"}\n')
        self.assertEqual(ctx.exception.error_class, "acp-failure")

    def test_initialize_handshake_against_fake_stdio_peer(self) -> None:
        from groklib import acp

        proc = subprocess.Popen(
            [sys.executable, "-u", str(self.peer_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        try:
            client = acp.AcpClient(proc, timeout_seconds=5)
            result = client.initialize()
            self.assertEqual(result.get("protocolVersion"), 1)
            caps = result.get("agentCapabilities") or {}
            self.assertTrue(caps.get("loadSession"))
            session = client.session_new(cwd="/tmp", mcp_servers=[])
            self.assertEqual(session.get("sessionId"), "sess-fake-1")
            chunks: list = []
            prompt_result = client.session_prompt(
                session_id="sess-fake-1",
                text="hi",
                on_update=lambda n: chunks.append(n),
            )
            self.assertEqual(prompt_result.get("stopReason"), "end_turn")
            self.assertEqual(len(chunks), 2)
            client.session_cancel(session_id="sess-fake-1")
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_initialize_client_info_uses_stable_plugin_version(self) -> None:
        """clientInfo.version must be the packaging id (2.0.0), not experimental."""
        from groklib import acp

        proc = subprocess.Popen(
            [sys.executable, "-u", str(self.peer_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        try:
            client = acp.AcpClient(proc, timeout_seconds=5)
            result = client.initialize()
            echoed = result.get("echoedClientInfo") or {}
            self.assertEqual(echoed.get("name"), "grok-skills-wrapper")
            self.assertEqual(echoed.get("version"), "2.0.0")
            self.assertNotEqual(echoed.get("version"), "experimental")
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_timeout_raises_acp_failure(self) -> None:
        from groklib import acp

        hang = pathlib.Path(self._tmpdir) / "hang_peer.py"
        hang.write_text(
            "import time, sys\n"
            "sys.stdin.readline()\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", str(hang)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        try:
            client = acp.AcpClient(proc, timeout_seconds=0.3)
            with self.assertRaises(GrokWrapperError) as ctx:
                client.initialize()
            self.assertEqual(ctx.exception.error_class, "acp-failure")
        finally:
            proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
