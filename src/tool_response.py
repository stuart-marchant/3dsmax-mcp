"""Structured result envelopes for MCP tool calls."""

from __future__ import annotations

import json
import time
from base64 import b64encode
from functools import wraps
from inspect import Parameter, Signature, signature
from pathlib import Path
from typing import Any, Callable, get_type_hints

from .helpers import audit


_ERROR_PREFIXES = (
    "error:",
    "unknown action:",
    "unsupported ",
    "no image files found",
    "no textures matched",
    "failed:",
    "failed to ",
    "blocked by safe mode",
    "maxscript error",
)
_ERROR_SUBSTRINGS = (" not found:",)


def _json_or_raw(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return value


def _coerce_warnings(result: Any) -> list[Any]:
    if isinstance(result, dict):
        warnings = result.get("warnings")
        if isinstance(warnings, list):
            return warnings
        if warnings:
            return [warnings]
    return []


def _json_safe(value: Any) -> Any:
    """Convert common MCP/Python return values into JSON-serializable data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "encoding": "base64",
            "size": len(value),
            "data": b64encode(value).decode("ascii"),
        }
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]

    to_image_content = getattr(value, "to_image_content", None)
    if callable(to_image_content):
        content = to_image_content()
        if hasattr(content, "model_dump"):
            data = content.model_dump(by_alias=True)
        elif hasattr(content, "dict"):
            data = content.dict(by_alias=True)
        else:
            data = dict(content)
        return {
            "type": "image",
            "mime_type": data.get("mimeType") or data.get("mime_type"),
            "encoding": "base64",
            "data": data.get("data"),
        }

    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    if hasattr(value, "dict"):
        return _json_safe(value.dict())
    return repr(value)


def _error_from_result(result: Any, raw: Any) -> dict[str, str] | None:
    if isinstance(result, dict):
        status = str(result.get("status", "")).lower()
        error = result.get("error")
        if status == "error" or error:
            return {
                "type": str(result.get("error_type") or result.get("type") or "ToolError"),
                "message": str(error or result.get("message") or raw),
            }
    if isinstance(raw, str):
        stripped = raw.strip()
        lowered = stripped.lower()
        if lowered.startswith(_ERROR_PREFIXES) or any(s in lowered for s in _ERROR_SUBSTRINGS):
            return {"type": "ToolError", "message": stripped}
    return None


def envelope_result(
    raw: Any,
    *,
    elapsed_ms: float,
    transport: dict[str, Any] | None = None,
) -> str:
    """Wrap a tool return value in a stable JSON envelope."""
    result = _json_or_raw(raw)
    error = _error_from_result(result, raw)
    payload: dict[str, Any] = {
        "ok": error is None,
        "result": _json_safe(result) if error is None else None,
        "warnings": _coerce_warnings(result),
        "error": error,
        "transport": transport,
        "elapsed_ms": round(elapsed_ms, 3),
    }
    if isinstance(result, dict):
        hint = result.get("hint")
        if hint:
            payload["hint"] = _json_safe(hint)
    return json.dumps(payload, ensure_ascii=False)


def envelope_exception(
    exc: BaseException,
    *,
    elapsed_ms: float,
    transport: dict[str, Any] | None = None,
) -> str:
    payload = {
        "ok": False,
        "result": None,
        "warnings": [],
        "error": {
            "type": exc.__class__.__name__,
            "message": str(exc),
        },
        "transport": transport,
        "elapsed_ms": round(elapsed_ms, 3),
    }
    return json.dumps(payload, ensure_ascii=False)


def make_structured_tool(
    fn: Callable[..., Any],
    *,
    transport_provider: Callable[[], dict[str, Any] | None] | None = None,
    before_call: Callable[[], None] | None = None,
) -> Callable[..., str]:
    """Return a wrapper that preserves the original tool schema/signature."""

    fn_signature: Signature = signature(fn)
    try:
        resolved_annotations = get_type_hints(fn, include_extras=True)
    except Exception:
        resolved_annotations = dict(getattr(fn, "__annotations__", {}))

    resolved_params: list[Parameter] = []
    for param in fn_signature.parameters.values():
        if param.name in resolved_annotations:
            resolved_params.append(param.replace(annotation=resolved_annotations[param.name]))
        else:
            resolved_params.append(param)
    if "return" in resolved_annotations:
        fn_signature = fn_signature.replace(
            parameters=resolved_params,
            return_annotation=resolved_annotations["return"],
        )
    else:
        fn_signature = fn_signature.replace(parameters=resolved_params)

    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> str:
        if before_call:
            before_call()
        # Argument size is captured *before* invocation so the audit
        # record reflects what the LLM tried to do even when the tool
        # raises. We only record the size; argument values themselves
        # never leave the process.
        try:
            arg_bytes = len(json.dumps(
                {"args": list(args), "kwargs": kwargs},
                ensure_ascii=False,
                default=repr,
            ).encode("utf-8"))
        except Exception:
            arg_bytes = 0
        started_at = time.perf_counter()
        try:
            raw = fn(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            transport = transport_provider() if transport_provider else None
            envelope = envelope_result(raw, elapsed_ms=elapsed_ms, transport=transport)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            transport = transport_provider() if transport_provider else None
            envelope = envelope_exception(exc, elapsed_ms=elapsed_ms, transport=transport)

        audit.emit(
            tool=fn.__name__,
            arg_bytes=arg_bytes,
            envelope_json=envelope,
            elapsed_ms=elapsed_ms,
        )
        return envelope

    wrapped.__signature__ = fn_signature  # type: ignore[attr-defined]
    wrapped.__annotations__ = resolved_annotations
    return wrapped
