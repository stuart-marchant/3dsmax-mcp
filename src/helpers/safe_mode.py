"""Defense-in-depth safe-mode filter applied to MAXScript before it reaches the bridge.

The native bridge and the MAXScript TCP listener each apply their own
case-insensitive substring blocklists. We mirror an expanded version of
that blocklist here so an LLM-generated script is rejected at the
Python boundary, before crossing into Max — even if a future code path
forgets to enforce safe_mode on the receiving side.

This is intentionally a *coarse* filter. It catches the common-shape
mistakes that an LLM hallucinates (``deleteFile``, ``DOSCommand``,
``dotNetClass "System.Diagnostics.Process"``, ``fileIn "..."``) but does
not pretend to be a sandbox. A determined author can still bypass with
string concatenation, runtime symbol lookup, or other MaxScript meta
features. See README.md → "Safe mode" for the full discussion.

Tiers:

* ``MCP_SAFE_MODE=strict`` (default for studio trial) — adds
  scene-destructive intrinsics (``resetMaxFile``, ``loadMaxFile``,
  ``meditMaterials[``, etc.) to the blocklist. Use this when the LLM
  driver is a model whose behaviour you do not fully trust yet.
* ``MCP_SAFE_MODE=true`` (legacy) — original blocklist only. Equivalent
  to leaving the env var unset.
* ``MCP_SAFE_MODE=false`` (or ``0`` / ``no`` / ``off``) — Python-side
  filter is disabled. The native bridge and MAXScript listener still
  enforce their own copies of the blocklist.
"""

from __future__ import annotations

import os
import re

# Keep this list in lockstep with:
#   - maxscript/mcp_server.ms : `blocked = #(...)`
#   - native/src/command_dispatcher.cpp : `static const char* blocked[]`
# If you add to any of them, add here too.
#
# Substrings are lowercased. The check uses ``substring in lower(script)``.
BLOCKED_FRAGMENTS: tuple[str, ...] = (
    # File / shell launchers from the original MAXScript blocklist.
    "doscommand",
    "hiddendoscommand",
    "shelllaunch",
    "deletefile",
    "createfile",
    # The bridge already routes Python execution through the safe_mode path;
    # do not let an MCP caller smuggle it in via execute_maxscript.
    "python.execute",
    # .NET reflection escape hatches. Without these, a model that has been
    # told "DOSCommand is blocked" simply pivots to
    # ``dotNetClass "System.Diagnostics.Process"``.
    "dotnetclass",
    "dotnetobject",
    "dotnetmethod",
    "loadassembly",
    # MAXScript code loaders that pull in arbitrary script files.
    "filein ",   # trailing space to avoid clobbering identifiers
    "filein\t",
    "filein\"",
    "filein(",
    # Generic registration of OLE/COM components.
    "registerolei",
    # Base64 / hash primitives often used to obfuscate payloads.
    "decodebase64",
    "encodebase64",
    # ``executeScript`` / ``executeString`` are meta-eval entry points
    # that take a code string and run it — bypasses the safe_mode
    # substring filter on the original input by indirection.
    "executescript",
    "executestring",
)

# Strict-mode extras: scene-destructive intrinsics that legitimate
# artist work never reaches via ``execute_maxscript`` (dedicated tools
# cover the same intent). Turning these off prevents an LLM from
# wiping the scene with a one-line script.
STRICT_BLOCKED_FRAGMENTS: tuple[str, ...] = (
    "resetmaxfile",
    "loadmaxfile",
    "holdmaxfile",
    "fetchmaxfile",
    "meditmaterials[",
)

# Regex matching only ``execute "..."`` / ``execute @"..."`` /
# ``execute(...)`` where the argument starts with a literal string.
# This is the dangerous shape — the LLM is handing a literal MAXScript
# fragment to be re-parsed and run, which is exactly how a model
# bypasses the substring filter (``execute ("DOS" + "Command")``).
# Benign ``execute (someVar)`` / ``execute (expr())`` passes through.
_EXECUTE_LITERAL_RE = re.compile(
    r"\bexecute\b\s*[(]?\s*(@?\"|@?')",
    re.IGNORECASE,
)


class SafeModeViolation(ValueError):
    """Raised when a script trips the safe-mode pre-check."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"Blocked by safe mode: {reason}. Set MCP_SAFE_MODE=false to "
            "disable the Python pre-check (the C++ bridge still enforces "
            "its own filter; set safe_mode=false in mcp_config.ini to "
            "disable that too)."
        )


def _mode() -> str:
    """Return ``"strict"``, ``"on"``, or ``"off"`` for the current env."""
    raw = (os.environ.get("MCP_SAFE_MODE") or "strict").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return "off"
    if raw in {"strict"}:
        return "strict"
    # Anything else (1, true, yes, on, unset → default) is the default "on".
    # In this fork, "on" is also strict — explicit choice for the trial.
    # If you want pre-trial behaviour, set MCP_SAFE_MODE=lenient.
    if raw == "lenient":
        return "on"
    return "strict"


def is_enabled() -> bool:
    return _mode() != "off"


def check_script(script: str) -> None:
    """Raise :class:`SafeModeViolation` if ``script`` is rejected.

    Three checks:

    1. The base substring blocklist (always-on).
    2. If strict mode, the scene-destructive intrinsics list.
    3. Always: ``execute "literal"`` / ``execute @"literal"`` /
       ``execute("literal")`` shape — these are how an LLM smuggles a
       string past the substring filter for re-parsing.
    """
    mode = _mode()
    if mode == "off":
        return

    lowered = script.lower()

    for fragment in BLOCKED_FRAGMENTS:
        if fragment in lowered:
            raise SafeModeViolation(
                f"script contains restricted fragment {fragment!r}"
            )

    if mode == "strict":
        for fragment in STRICT_BLOCKED_FRAGMENTS:
            if fragment in lowered:
                raise SafeModeViolation(
                    f"script contains scene-destructive intrinsic "
                    f"{fragment!r} (MCP_SAFE_MODE=strict). Use a dedicated "
                    "tool — manage_scene / delete_objects / replace_material — "
                    "or set MCP_SAFE_MODE=lenient."
                )

    if _EXECUTE_LITERAL_RE.search(script):
        raise SafeModeViolation(
            "script uses execute on a string literal "
            "(execute \"…\" / execute @\"…\" / execute(\"…\")). "
            "Re-parsing a literal MAXScript fragment defeats the "
            "safe-mode substring filter. Inline the call instead "
            "or pass the value as an expression, not a string."
        )
