"""Path-input validation for MCP tools that accept filesystem paths.

The MCP tool surface is driven by an LLM. Tools like ``render_scene``,
``merge_from_file``, ``inspect_max_file``, ``batch_file_info`` and
``search_max_files`` all take string paths that get handed to the native
bridge (or to MAXScript) which then reads/writes the disk with the
current Max process's full privileges.

This module gates those inputs so a hallucinated or prompt-injected path
cannot reach into the user's profile, system folders, or arbitrary
network shares unless the operator has explicitly allowed it.

Configuration (read at first call, cached):

* ``MCP_PROJECT_ROOTS`` — semicolon- (Windows) or colon-separated list
  of absolute directories that tools are allowed to read from / write
  to. When set, this is the only allow-list.

* ``MCP_ALLOW_ANY_PATH`` — set to ``true`` / ``1`` / ``yes`` to disable
  validation entirely (escape hatch for power users who know what they
  are doing).

When neither is set, a permissive default is used: the user's profile
``Documents`` / ``Desktop`` / ``Downloads`` plus the system temp
directory. A small list of always-denied prefixes (Windows / SSH / GPG
secrets, system32, etc.) is applied in *all* modes except
``MCP_ALLOW_ANY_PATH``.

Validators raise ``PathPolicyError`` (a ``ValueError``) on rejection so
MCP tools can surface a clear envelope error to the caller.
"""

from __future__ import annotations

import os
import platform
from functools import lru_cache
from pathlib import Path, PurePath
from typing import Iterable


class PathPolicyError(ValueError):
    """Raised when a tool argument violates the path policy."""


# Sensitive locations we always refuse to touch, even when project roots
# are wide-open. These are substring checks on the *resolved, lowercased*
# path so simple obfuscation (case, trailing slashes) does not bypass.
_ALWAYS_DENY_FRAGMENTS = (
    # POSIX-style
    "/.ssh/",
    "/.aws/",
    "/.gnupg/",
    "/.config/gcloud/",
    "/.kube/",
    "/.docker/",
    "/.netrc",
    "/.npmrc",
    "/.pypirc",
    "/etc/passwd",
    "/etc/shadow",
    # Windows-style (normalized to forward slashes by _normalize)
    "/windows/system32/",
    "/windows/syswow64/",
    "/system volume information/",
    "/programdata/microsoft/crypto/",
    "/appdata/roaming/microsoft/credentials/",
    "/appdata/roaming/microsoft/protect/",
    "/appdata/local/microsoft/credentials/",
)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize(p: Path) -> str:
    """Resolve and return a forward-slash, lowercase string for comparison.

    ``Path.resolve(strict=False)`` follows symlinks where possible and
    collapses ``..`` segments. We keep the result string-only so callers
    that pass non-existent output paths (e.g., ``render_scene``) do not
    hit ``FileNotFoundError``.
    """
    try:
        resolved = p.resolve(strict=False)
    except OSError:
        resolved = p
    s = str(resolved).replace("\\", "/")
    return s.lower()


@lru_cache(maxsize=1)
def _policy() -> dict:
    """Return the effective policy snapshot (cached for process lifetime).

    Tests can call ``_policy.cache_clear()`` to reload after mutating
    env vars; production callers should restart the MCP server.
    """
    allow_any = _truthy(os.environ.get("MCP_ALLOW_ANY_PATH"))

    raw_roots = os.environ.get("MCP_PROJECT_ROOTS", "").strip()
    explicit_roots: list[str] = []
    if raw_roots:
        sep = ";" if platform.system() == "Windows" else os.pathsep
        # Tolerate either separator on either OS — artists copy-paste
        # paths between Windows installers and Mac/Linux dev shells.
        parts = []
        for chunk in raw_roots.split(sep):
            parts.extend(chunk.split(";" if sep != ";" else ":"))
        explicit_roots = [_normalize(Path(p)) for p in parts if p.strip()]

    default_roots: list[str] = []
    if not explicit_roots and not allow_any:
        home = Path.home()
        for sub in ("Documents", "Desktop", "Downloads", "3dsMax", "Max"):
            candidate = home / sub
            default_roots.append(_normalize(candidate))
        # System temp — capture_* writes here; render_scene may too.
        import tempfile
        default_roots.append(_normalize(Path(tempfile.gettempdir())))

    return {
        "allow_any": allow_any,
        "explicit_roots": explicit_roots,
        "default_roots": default_roots,
    }


def _has_root_match(normalized: str, roots: Iterable[str]) -> bool:
    for root in roots:
        if not root:
            continue
        # Require a directory boundary so "/projects" does not match
        # "/projects_secret".
        root_with_sep = root if root.endswith("/") else root + "/"
        if normalized == root or normalized.startswith(root_with_sep):
            return True
    return False


def _check_deny_fragments(normalized: str) -> None:
    for frag in _ALWAYS_DENY_FRAGMENTS:
        if frag in normalized:
            raise PathPolicyError(
                f"Path is in a sensitive location and blocked by policy: {normalized} "
                f"(matched fragment {frag!r}). Move the file out of this directory, "
                "or set MCP_ALLOW_ANY_PATH=true to disable the policy."
            )


def validate_path(value: str, *, purpose: str = "access") -> str:
    """Return ``value`` unchanged if it satisfies the path policy.

    ``purpose`` is a short verb used in error messages ("read", "write",
    "scan"). The caller is responsible for choosing it.
    """
    if value is None or value == "":
        # Empty paths are passed through — render_scene treats "" as
        # "render to VFB without saving" and the file tools treat it as
        # a no-op. The native handlers handle the empty case.
        return value

    raw = str(value).strip()
    if not raw:
        return value

    # Refuse UNC / extended-length / device paths outright — the LLM has
    # no legitimate reason to construct these, and they make policy
    # comparisons brittle.
    if raw.startswith(("\\\\", "//", "\\??\\", "\\\\?\\")):
        raise PathPolicyError(
            f"UNC and device paths are not allowed ({raw!r}); use a local path."
        )

    path = Path(raw)
    normalized = _normalize(path)

    policy = _policy()

    if policy["allow_any"]:
        # Even the escape hatch refuses obvious credential paths.
        _check_deny_fragments(normalized)
        return value

    _check_deny_fragments(normalized)

    roots = policy["explicit_roots"] or policy["default_roots"]
    if not roots:
        raise PathPolicyError(
            "No allowed project roots configured. Set MCP_PROJECT_ROOTS "
            "or MCP_ALLOW_ANY_PATH=true."
        )
    if not _has_root_match(normalized, roots):
        roots_str = ", ".join(roots)
        raise PathPolicyError(
            f"Path is outside the allowed roots and cannot be used to {purpose}: "
            f"{raw!r}. Allowed roots: {roots_str}. Set MCP_PROJECT_ROOTS to add "
            "more, or MCP_ALLOW_ANY_PATH=true to disable the policy."
        )

    return value


def validate_paths(values: Iterable[str], *, purpose: str = "access") -> list[str]:
    return [validate_path(v, purpose=purpose) for v in values]
