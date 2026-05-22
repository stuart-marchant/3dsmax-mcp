"""Tests for the MCP tool-call audit log.

Verifies (1) the JSONL shape that hits disk, (2) the MCP_DISABLE_AUDIT
opt-out, (3) no argument values or result bodies leak into the record,
(4) that the audit hook is invoked from the structured-tool wrapper —
the path the live MCP server actually uses.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.helpers import audit
from src.tool_response import make_structured_tool


class _EnvAndDirSandbox:
    """Scope an MCP_AUDIT_DIR + opt-out env to a single test."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_env: dict[str, str | None] = {}

    def __enter__(self) -> "_EnvAndDirSandbox":
        for key in ("MCP_AUDIT_DIR", "MCP_DISABLE_AUDIT"):
            self._saved_env[key] = os.environ.get(key)
        os.environ["MCP_AUDIT_DIR"] = str(self.dir)
        os.environ.pop("MCP_DISABLE_AUDIT", None)
        return self

    def __exit__(self, *_exc) -> None:
        for key, val in self._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self._tmp.cleanup()

    def read_today(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        path = self.dir / f"tool_calls-{now.strftime('%Y-%m-%d')}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


class AuditEmitTests(unittest.TestCase):
    def test_emit_writes_well_formed_jsonl_line(self) -> None:
        with _EnvAndDirSandbox() as sb:
            envelope = json.dumps({
                "ok": True,
                "result": {"value": 1},
                "warnings": [],
                "error": None,
                "transport": {"primary": "native"},
                "elapsed_ms": 1.0,
            })
            audit.emit(tool="get_scene_info", arg_bytes=42, envelope_json=envelope, elapsed_ms=12.5)

            lines = sb.read_today()
            self.assertEqual(len(lines), 1)
            rec = lines[0]

        self.assertEqual(rec["tool"], "get_scene_info")
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["arg_bytes"], 42)
        self.assertEqual(rec["error_type"], None)
        self.assertEqual(rec["transport"], "native")
        self.assertEqual(rec["elapsed_ms"], 12.5)
        self.assertGreater(rec["result_bytes"], 0)
        # Schema: nothing beyond the documented keys
        self.assertEqual(
            set(rec.keys()),
            {"ts", "tool", "ok", "elapsed_ms", "arg_bytes", "result_bytes",
             "error_type", "transport"},
        )

    def test_emit_records_error_type_on_failure(self) -> None:
        with _EnvAndDirSandbox() as sb:
            envelope = json.dumps({
                "ok": False,
                "result": None,
                "warnings": [],
                "error": {"type": "PathPolicyError", "message": "denied"},
                "transport": None,
                "elapsed_ms": 0.5,
            })
            audit.emit(tool="merge_from_file", arg_bytes=200, envelope_json=envelope, elapsed_ms=0.5)

            rec = sb.read_today()[0]

        self.assertFalse(rec["ok"])
        self.assertEqual(rec["error_type"], "PathPolicyError")
        self.assertIsNone(rec["transport"])

    def test_opt_out_disables_writes(self) -> None:
        with _EnvAndDirSandbox() as sb:
            os.environ["MCP_DISABLE_AUDIT"] = "true"
            audit.emit(tool="x", arg_bytes=1, envelope_json='{"ok":true}', elapsed_ms=0.1)
            self.assertEqual(sb.read_today(), [])

    def test_audit_does_not_leak_payloads(self) -> None:
        """Even when the envelope contains secrets, only sizes are written."""
        secret_env = json.dumps({
            "ok": True,
            "result": {"api_key": "sk-ant-SECRET-VALUE", "scene": "B" * 5000},
            "warnings": [],
            "error": None,
            "transport": {"primary": "native"},
            "elapsed_ms": 1.0,
        })
        with _EnvAndDirSandbox() as sb:
            audit.emit(tool="leaky_tool", arg_bytes=1, envelope_json=secret_env, elapsed_ms=1.0)
            on_disk = (sb.dir.glob("tool_calls-*.jsonl").__next__()).read_text("utf-8")

        self.assertNotIn("sk-ant-SECRET-VALUE", on_disk)
        self.assertNotIn("BBBBB", on_disk)

    def test_write_failure_does_not_raise(self) -> None:
        """A bad log dir should degrade silently, not break the tool call."""
        with _EnvAndDirSandbox() as sb:
            # Point at a path that cannot be created (parent is a file).
            blocker = sb.dir / "blocker"
            blocker.write_text("not a dir")
            os.environ["MCP_AUDIT_DIR"] = str(blocker / "logs")
            try:
                audit.emit(tool="x", arg_bytes=1, envelope_json='{"ok":true}', elapsed_ms=0.1)
            except Exception as exc:
                self.fail(f"audit.emit raised on write failure: {exc!r}")


class StructuredToolIntegrationTests(unittest.TestCase):
    """End-to-end: the MCP wrapper writes one audit line per call."""

    def test_wrapper_emits_one_line_per_call(self) -> None:
        def my_tool(x: int, y: int = 0) -> str:
            return json.dumps({"value": x + y})

        wrapped = make_structured_tool(my_tool)

        with _EnvAndDirSandbox() as sb:
            envelope = json.loads(wrapped(3, y=4))
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["result"]["value"], 7)

            lines = sb.read_today()
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["tool"], "my_tool")
            self.assertTrue(lines[0]["ok"])
            self.assertGreater(lines[0]["arg_bytes"], 0)

    def test_wrapper_emits_record_when_tool_raises(self) -> None:
        def broken_tool() -> str:
            raise RuntimeError("boom")

        wrapped = make_structured_tool(broken_tool)

        with _EnvAndDirSandbox() as sb:
            envelope = json.loads(wrapped())
            self.assertFalse(envelope["ok"])

            rec = sb.read_today()[0]

        self.assertEqual(rec["tool"], "broken_tool")
        self.assertEqual(rec["error_type"], "RuntimeError")
        self.assertFalse(rec["ok"])


if __name__ == "__main__":
    unittest.main()
