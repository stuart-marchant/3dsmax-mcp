"""Lockstep check between the three safe-mode blocklists.

`src/helpers/safe_mode.py::BLOCKED_FRAGMENTS`,
`native/src/command_dispatcher.cpp::blocked[]`, and
`maxscript/mcp_server.ms`'s ``local blocked = #(...)`` array must contain
the same fragments. If they drift, an LLM-generated script blocked at
one layer can slip past another — which is exactly what the bug shape
"someone added a fragment to one list and forgot the others" looks
like in practice.

We parse the C++ and MAXScript sources textually (not via build
artefacts) so this test runs in any environment, including macOS dev
boxes without the Windows / Max SDK.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.helpers.safe_mode import BLOCKED_FRAGMENTS

ROOT = Path(__file__).resolve().parent.parent
CPP_PATH = ROOT / "native" / "src" / "command_dispatcher.cpp"
MS_PATH = ROOT / "maxscript" / "mcp_server.ms"


# Both C++ and MAXScript use double-quoted string literals with ``\``
# escape sequences. We only need to handle the escapes that actually
# appear in the blocklist (``\"`` and ``\t``); a general C-string
# unescape isn't necessary.
_ESCAPES = {
    '\\"': '"',
    "\\t": "\t",
    "\\\\": "\\",
}


def _unescape(literal: str) -> str:
    out = []
    i = 0
    while i < len(literal):
        ch = literal[i]
        if ch == "\\" and i + 1 < len(literal):
            two = literal[i:i + 2]
            if two in _ESCAPES:
                out.append(_ESCAPES[two])
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# Matches a double-quoted string with escape support. Captures the body
# between the quotes; the caller is responsible for unescaping.
_DQ_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _extract_cpp_blocklist(source: str) -> tuple[str, ...]:
    """Pull every string literal out of the ``blocked[]`` array body."""
    match = re.search(
        r"static\s+const\s+char\*\s+blocked\s*\[\s*\]\s*=\s*\{(.*?)\}\s*;",
        source,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(
            "Could not locate `static const char* blocked[] = { ... };` "
            "in command_dispatcher.cpp — has the declaration changed shape?"
        )
    body = match.group(1)
    return tuple(_unescape(m) for m in _DQ_STRING.findall(body))


def _extract_maxscript_blocklist(source: str) -> tuple[str, ...]:
    """Pull every string literal out of the ``local blocked = #(...)`` array."""
    match = re.search(
        r"local\s+blocked\s*=\s*#\((.*?)\)",
        source,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(
            "Could not locate `local blocked = #( ... )` in mcp_server.ms"
        )
    body = match.group(1)
    return tuple(_unescape(m) for m in _DQ_STRING.findall(body))


class SafeModeLockstepTests(unittest.TestCase):
    """Fail loudly when any of the three blocklists drifts from the others."""

    def setUp(self) -> None:
        self.python_set = set(BLOCKED_FRAGMENTS)
        self.cpp_set = set(_extract_cpp_blocklist(CPP_PATH.read_text(encoding="utf-8")))
        self.ms_set = set(_extract_maxscript_blocklist(MS_PATH.read_text(encoding="utf-8")))

    def test_python_and_cpp_match(self) -> None:
        only_python = sorted(self.python_set - self.cpp_set)
        only_cpp = sorted(self.cpp_set - self.python_set)
        self.assertFalse(
            only_python or only_cpp,
            f"Python and C++ safe_mode blocklists drifted.\n"
            f"  Only in Python: {only_python}\n"
            f"  Only in C++:    {only_cpp}\n"
            f"Update src/helpers/safe_mode.py::BLOCKED_FRAGMENTS and "
            f"native/src/command_dispatcher.cpp::blocked[] together.",
        )

    def test_python_and_maxscript_match(self) -> None:
        only_python = sorted(self.python_set - self.ms_set)
        only_ms = sorted(self.ms_set - self.python_set)
        self.assertFalse(
            only_python or only_ms,
            f"Python and MAXScript safe_mode blocklists drifted.\n"
            f"  Only in Python:    {only_python}\n"
            f"  Only in MAXScript: {only_ms}\n"
            f"Update src/helpers/safe_mode.py::BLOCKED_FRAGMENTS and "
            f"maxscript/mcp_server.ms `local blocked = #(...)` together.",
        )

    def test_cpp_and_maxscript_match(self) -> None:
        # Implied by the other two but cheap to assert directly so the
        # failure message points at the right pair.
        only_cpp = sorted(self.cpp_set - self.ms_set)
        only_ms = sorted(self.ms_set - self.cpp_set)
        self.assertFalse(
            only_cpp or only_ms,
            f"C++ and MAXScript safe_mode blocklists drifted.\n"
            f"  Only in C++:       {only_cpp}\n"
            f"  Only in MAXScript: {only_ms}",
        )

    def test_all_lists_are_non_empty(self) -> None:
        # Guard against the regex silently matching an empty body.
        self.assertGreater(len(self.python_set), 0)
        self.assertGreater(len(self.cpp_set), 0)
        self.assertGreater(len(self.ms_set), 0)


if __name__ == "__main__":
    unittest.main()
