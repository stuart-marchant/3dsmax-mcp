"""Path-input validation for MCP tools that accept filesystem paths.

The MCP tool surface is driven by an LLM. Tools like ``render_scene``,
``merge_from_file``, ``inspect_max_file``, ``batch_file_info`` and
``search_max_files`` all take string paths that get handed to the native
bridge (or to MAXScript) which then reads/writes the disk with the
current Max process's full privileges.

This module gates those inputs so a hallucinated or prompt-injected path
cannot reach into the user's profile, system folders, or arbitrary
network shares unless the operator has explicitly allowed it.

Configuration (re-read on every call so env changes do not require an
MCP server restart):

* ``MCP_PROJECT_ROOTS`` — list of absolute directories that tools are
  allowed to read from / write to.

  - On Windows, separate entries with ``;`` (a colon is part of a
    drive letter, e.g. ``C:\\Projects``).
  - On Linux / macOS, separate entries with ``os.pathsep`` (``:``)
    or ``;`` (both accepted).

  When set, this is the only allow-list. UNC paths (``\\\\server\\share\\…``)
  are accepted *only* when an entry in this list is itself a UNC root
  that the path lives under — studios that work off network shares
  must name them explicitly.

* ``MCP_ALLOW_ANY_PATH`` — set to ``true`` / ``1`` / ``yes`` to disable
  validation entirely (escape hatch for power users who know what they
  are doing). UNC paths are *still* refused under this flag because
  there is no allow-list to anchor them to.

When neither is set, a permissive default is used: the user's profile
``Documents`` / ``Desktop`` / ``Downloads``, plus the Autodesk 3ds Max
default scene/autoback/export/import folders that ship under
``%USERPROFILE%\\Documents\\3dsMax``, plus the system temp directory.
A small list of always-denied prefixes (Windows / SSH / GPG secrets,
system32, etc.) is applied in *all* modes including
``MCP_ALLOW_ANY_PATH``.

Validators raise ``PathPolicyError`` (a ``ValueError``) on rejection so
MCP tools can surface a clear envelope error to the caller.
"""

from __future__ import annotations

import os
from pathlib import Path
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


def _normalize_unc(raw: str) -> str:
    """Normalize a UNC path (``\\\\server\\share\\dir``) for comparison.

    ``Path.resolve`` is unreliable on UNC paths across platforms (and
    is a no-op on macOS where UNC isn't a real concept), so we
    string-normalize: collapse runs of slashes, lowercase, drop
    trailing slash. ``..`` segments are left alone — UNC paths should
    not be passing through this code path on machines that resolve
    them.
    """
    s = raw.replace("\\", "/")
    # Collapse runs of slashes, but preserve the leading ``//`` UNC marker.
    leading = ""
    if s.startswith("//"):
        leading = "//"
        s = s[2:]
    while "//" in s:
        s = s.replace("//", "/")
    s = leading + s
    if s.endswith("/"):
        s = s[:-1]
    return s.lower()


def _split_project_roots(raw: str) -> list[str]:
    """Split ``MCP_PROJECT_ROOTS`` into individual root paths.

    The hard rule: a Windows drive letter is followed by ``:`` (e.g.
    ``C:\\Projects``). So on Windows the entry separator must be ``;``.
    On POSIX both ``:`` and ``;`` are accepted because there are no
    drive letters and artists commonly copy/paste env vars across
    shells.
    """
    if os.name == "nt":
        parts = raw.split(";")
    else:
        # Replace ; with : then split — accepts either separator.
        parts = raw.replace(";", os.pathsep).split(os.pathsep)
    return [p.strip() for p in parts if p.strip()]


def _is_unc(raw: str) -> bool:
    return raw.startswith(("\\\\", "//", "\\??\\", "\\\\?\\"))


def _default_roots() -> list[str]:
    """Roots used when MCP_PROJECT_ROOTS is unset.

    Includes the 3ds Max default folders that ship under
    ``%USERPROFILE%\\Documents\\3dsMax`` (scenes, autoback, export,
    import, vpost, previews, sceneassets) — without these, an artist
    following Max's own convention hits a wall on the first
    ``inspect_max_file`` or ``render_scene``.
    """
    home = Path.home()
    roots: list[str] = []
    for sub in ("Documents", "Desktop", "Downloads"):
        roots.append(_normalize(home / sub))
    # Max default working tree. The folder names match Autodesk's
    # defaults; the user's actual `Documents` is set elsewhere on a
    # corp-managed laptop and `Path.home()/Documents` may not be it,
    # but adding these is harmless when they don't exist.
    max_subs = (
        "Documents/3dsMax",
        "Documents/3dsMax/scenes",
        "Documents/3dsMax/autoback",
        "Documents/3dsMax/export",
        "Documents/3dsMax/import",
        "Documents/3dsMax/vpost",
        "Documents/3dsMax/previews",
        "Documents/3dsMax/sceneassets",
    )
    for sub in max_subs:
        roots.append(_normalize(home / sub))
    # Legacy / personal-folder shapes some artists still use.
    for sub in ("3dsMax", "Max"):
        roots.append(_normalize(home / sub))
    # System temp — capture_* writes here; render_scene may too.
    import tempfile
    roots.append(_normalize(Path(tempfile.gettempdir())))
    return roots


def _policy() -> dict:
    """Compute the effective policy snapshot from current env vars.

    Re-read every call so artists who edit ``MCP_PROJECT_ROOTS`` /
    ``MCP_ALLOW_ANY_PATH`` see the change without restarting the MCP
    server. The work is cheap (a few env lookups + small list ops);
    the cost is dwarfed by the IPC round-trip that follows.
    """
    allow_any = _truthy(os.environ.get("MCP_ALLOW_ANY_PATH"))

    raw_roots = os.environ.get("MCP_PROJECT_ROOTS", "").strip()
    explicit_roots: list[str] = []
    explicit_unc_roots: list[str] = []
    if raw_roots:
        for part in _split_project_roots(raw_roots):
            if _is_unc(part):
                # Keep UNC roots both in the general list (so an inside
                # path matches) and in a dedicated list (so the UNC
                # path-prefix check below can permit a path whose root
                # was named explicitly).
                norm = _normalize_unc(part)
                explicit_roots.append(norm)
                explicit_unc_roots.append(norm)
            else:
                explicit_roots.append(_normalize(Path(part)))

    return {
        "allow_any": allow_any,
        "explicit_roots": explicit_roots,
        "explicit_unc_roots": explicit_unc_roots,
        "default_roots": _default_roots() if (not explicit_roots and not allow_any) else [],
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
                f"Path is in a sensitive location and blocked by policy: "
                f"{normalized} (matched fragment {frag!r}). This is a hard "
                "deny — credential, SSH, and system folders are never "
                "accessible to MCP tools. Move the file out of this "
                "directory if it is benign."
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

    # Device paths (``\\?\…`` / ``\??\…``) are unconditionally refused —
    # the LLM has no legitimate reason to construct these and they make
    # policy comparisons brittle.
    if raw.startswith(("\\??\\", "\\\\?\\")):
        raise PathPolicyError(
            f"Device paths are not allowed ({raw!r}); use a regular path."
        )

    policy = _policy()

    is_unc = _is_unc(raw)
    if is_unc:
        normalized = _normalize_unc(raw)
    else:
        normalized = _normalize(Path(raw))

    if is_unc:
        # UNC paths are only allowed when the operator named the share
        # explicitly in MCP_PROJECT_ROOTS. The escape hatch
        # MCP_ALLOW_ANY_PATH does NOT extend to UNC because there is no
        # safe default; a malicious prompt could otherwise reach any
        # internal share the user has credentials for.
        if not _has_root_match(normalized, policy["explicit_unc_roots"]):
            raise PathPolicyError(
                f"UNC path {raw!r} is not under any allow-listed share. "
                "Add the share root to MCP_PROJECT_ROOTS (e.g. "
                "'MCP_PROJECT_ROOTS=\\\\\\\\nas\\\\projects;D:\\\\Work')."
            )
        _check_deny_fragments(normalized)
        return value

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
            f"Path is outside the allowed project roots and cannot be used "
            f"to {purpose}: {raw!r}. Allowed roots: {roots_str}. Ask your "
            "TD to add this folder to MCP_PROJECT_ROOTS."
        )

    return value


def validate_paths(values: Iterable[str], *, purpose: str = "access") -> list[str]:
    return [validate_path(v, purpose=purpose) for v in values]
