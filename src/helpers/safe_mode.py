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
string concatenation, MaxScript ``execute``, or runtime symbol lookup.
See README.md → "Safe mode" for the full discussion.

Disable with the ``MCP_SAFE_MODE`` env var set to ``false`` (intentional
escape hatch for power users running on isolated dev boxes).
"""

from __future__ import annotations

import os

# Keep this list in lockstep with:
#   - maxscript/mcp_server.ms : `blocked = #(...)`
#   - native/src/command_dispatcher.cpp : `static const char* blocked[]`
# If you add to any of them, add here too.
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
    # Common .NET reflection escape hatches. Without these, a model that
    # has been told "DOSCommand is blocked" simply pivots to
    # ``dotNetClass "System.Diagnostics.Process"``.
    "dotnetclass",
    "dotnetobject",
    "dotnetmethod",
    "loadassembly",
    # MAXScript code loaders that pull in arbitrary script files.
    "filein ",   # trailing space to avoid clobbering identifiers ending in "filein"
    "filein\t",
    "filein\"",
    "filein(",
    # Generic registration of OLE/COM components.
    "registerolei",
    # Base64 / hash primitives often used to obfuscate payloads.
    "decodebase64",
    "encodebase64",
    # `execute` itself is a meta-eval that lets script smuggle anything
    # past keyword filters; reject the common shapes but leave bare
    # `execute` (legitimately used to evaluate expressions) untouched.
    "execute (",
    "execute(",
)


class SafeModeViolation(ValueError):
    """Raised when a script trips the safe-mode pre-check."""

    def __init__(self, fragment: str) -> None:
        self.fragment = fragment
        super().__init__(
            f"Blocked by safe mode: script contains a restricted fragment "
            f"({fragment!r}). Set MCP_SAFE_MODE=false to disable the Python "
            "pre-check (the C++ bridge still enforces its own filter; set "
            "safe_mode=false in mcp_config.ini to disable that too)."
        )


def is_enabled() -> bool:
    value = (os.environ.get("MCP_SAFE_MODE") or "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def check_script(script: str) -> None:
    """Raise :class:`SafeModeViolation` if ``script`` contains a blocked fragment."""
    if not is_enabled():
        return
    lowered = script.lower()
    for fragment in BLOCKED_FRAGMENTS:
        if fragment in lowered:
            raise SafeModeViolation(fragment)
