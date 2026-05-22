"""SIEM-friendly JSONL audit log of every MCP tool call.

Every wrapped MCP tool emits exactly one line to a per-day file under

* ``%LOCALAPPDATA%\\3dsmax-mcp\\logs\\`` on Windows
* ``$XDG_STATE_HOME/3dsmax-mcp/logs/`` (or ``~/.local/state/...``) elsewhere

The line is a single JSON object with this schema:

    {
      "ts":          "2026-05-22T03:14:15.926Z",   // ISO-8601 UTC
      "tool":        "render_scene",
      "ok":          true,
      "elapsed_ms":  42.7,
      "arg_bytes":   128,                          // size only, never payloads
      "result_bytes":15872,
      "error_type":  null,                          // string when ok=false
      "transport":   "native"                       // or "maxscript"/null
    }

We log **sizes only** — no argument values, no result bodies, no chat
prompts, no API keys. The point is post-incident traceability ("on
Tuesday at 14:03 tool X ran, was tool Y called within 10 seconds?")
without producing a secondary data-loss vector. The companion
LLM-call audit log (GHOTI-248) covers the chat egress story.

Operators can opt out by setting ``MCP_DISABLE_AUDIT=true``. The audit
log itself never throws — write failures degrade silently so a bad
log dir cannot break tool calls.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_logger = logging.getLogger(__name__)

# Single lock for the file append so concurrent tool calls (the MCP
# server is single-process stdio, but the FastMCP runtime can schedule
# tool execution off the main thread) don't interleave lines.
_write_lock = threading.Lock()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    return not _truthy(os.environ.get("MCP_DISABLE_AUDIT"))


def _log_root_override() -> Path | None:
    """Honour ``MCP_AUDIT_DIR`` so studios can pipe to a custom path."""
    raw = os.environ.get("MCP_AUDIT_DIR", "").strip()
    return Path(raw) if raw else None


def _default_log_root() -> Path:
    """Platform-appropriate default for the audit log directory.

    Windows is the deploy target so we honour ``%LOCALAPPDATA%`` first.
    On macOS / Linux dev boxes we fall back to the XDG state directory
    so the log doesn't pollute the user's home directory.
    """
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "3dsmax-mcp" / "logs"
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / "3dsmax-mcp" / "logs"


def log_dir() -> Path:
    """Return the resolved audit log directory (does not create it)."""
    return (_log_root_override() or _default_log_root()).resolve()


def _current_log_path(now: datetime) -> Path:
    return log_dir() / f"tool_calls-{now.strftime('%Y-%m-%d')}.jsonl"


def _safe_int(value: Any) -> int:
    """Return ``len(str(value))`` in bytes (utf-8) without raising."""
    try:
        if value is None:
            return 0
        if isinstance(value, (bytes, bytearray)):
            return len(value)
        if isinstance(value, str):
            return len(value.encode("utf-8"))
        # Coerce dicts / lists / ints / etc. via repr-of-json — we only
        # want an order-of-magnitude size; precision is irrelevant.
        return len(json.dumps(value, default=repr).encode("utf-8"))
    except Exception:
        return 0


def _normalise_transport(transport: Any) -> str | None:
    """Reduce the envelope's transport object to a single string label.

    The envelope passes a dict (e.g. ``{"primary": "native"}``); SIEM
    ingestion is happier with a flat string. ``None`` stays ``None``.
    """
    if transport is None:
        return None
    if isinstance(transport, str):
        return transport
    if isinstance(transport, dict):
        for key in ("primary", "name", "kind", "transport"):
            v = transport.get(key)
            if isinstance(v, str) and v:
                return v
        # Fall back to a stable compact rep when we don't recognise the shape.
        try:
            return json.dumps(transport, sort_keys=True, separators=(",", ":"))
        except Exception:
            return repr(transport)
    return str(transport)


def emit(
    *,
    tool: str,
    arg_bytes: int,
    envelope_json: str,
    elapsed_ms: float,
) -> None:
    """Append one audit line for a wrapped MCP tool call.

    ``envelope_json`` is the structured envelope the wrapper just
    produced; we read ``ok``, ``error.type``, and ``transport`` out of
    it (rather than ask the caller to pass them again) so the schema
    stays in lockstep with whatever the envelope reports.

    Never raises. A failed write is logged at WARN and the call
    continues.
    """
    if not is_enabled():
        return

    try:
        envelope = json.loads(envelope_json) if envelope_json else {}
    except Exception:
        envelope = {}

    error = envelope.get("error") if isinstance(envelope, dict) else None
    error_type: str | None = None
    if isinstance(error, dict):
        et = error.get("type")
        error_type = str(et) if et else None
    elif isinstance(error, str) and error:
        error_type = "ToolError"

    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "tool": tool,
        "ok": bool(envelope.get("ok")) if isinstance(envelope, dict) else False,
        "elapsed_ms": round(float(elapsed_ms), 3),
        "arg_bytes": int(arg_bytes),
        "result_bytes": _safe_int(envelope_json),
        "error_type": error_type,
        "transport": _normalise_transport(envelope.get("transport") if isinstance(envelope, dict) else None),
    }

    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"

    try:
        now = datetime.now(timezone.utc)
        path = _current_log_path(now)
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Open in append mode so concurrent processes (e.g. an
            # accidentally-launched second MCP server) interleave at the
            # line level on POSIX. Windows append is also atomic for
            # writes <= PIPE_BUF, which our line easily satisfies.
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except OSError as exc:
        _logger.warning("3dsmax-mcp: failed to write audit log: %s", exc)
