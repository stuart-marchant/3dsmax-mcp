# 3dsmax-mcp

<p align="left">
  <img src="images/logo.png" alt="3dsmax-mcp logo" width="200">
</p>

A production oriented MCP server that connects AI agents to Autodesk 3ds Max.
Works with any MCP-compatible client.

### Features

- **Native C++ Bridge** — 76 handlers running inside 3ds Max as a GUP plugin, 86-130x faster than MAXScript
- **One-step installer** — `uv run python install.py` handles everything
- **Quad-view capture** — Screenshotting is fast and supports multi views.
- **Full default tool surface** — full MCP profile exposes core and specialty scene/object/material/pipeline tools by default
- **116 tools in full profile** across scene, objects, materials, modifiers, controllers, viewport, introspection.
- **Bundled MAXScript reference** — 10 topic files for agents to write correct MAXScript

## Architecture

```
Agent  <-->  FastMCP (Python/stdio)  <-->  Named Pipe  <-->  C++ GUP Plugin  <-->  3ds Max SDK
                                      |
                                      +--> TCP:8765 fallback --> MAXScript listener
```

The native bridge runs inside 3ds Max as a Global Utility Plugin. It reads the scene graph directly through the C++ SDK and communicates over Windows named pipes. 76 native handlers for scene, objects, materials, modifiers, controllers, viewport, introspection, and more.

## Requirements

- [Python 3.10+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/)
- Autodesk 3ds Max 2026 (2024/2025 supported via MAXScript fallback)

## Installation

```powershell
git clone https://github.com/cl0nazepamm/3dsmax-mcp.git
cd 3dsmax-mcp
uv sync
uv run python install.py
```

## Updating

```powershell
git pull
uv sync
uv run python install.py
```

To install without copying or building the agent skill files:
```powershell
uv run python install.py --skip-skill
```

## MCP Tool Profile

The external MCP server defaults to the full profile so specialty modules such
as Data Channel, effects, floor-plan generation, tyFlow, RailClone, wire params,
and standalone-chat driver tools are available without extra setup. Use the core
profile only when you want a smaller common-tool surface.

```powershell
$env:MCP_TOOL_PROFILE = "core"
python -m src.server
```

## Skill

The skill file teaches agents how to use the tools, what pitfalls to avoid, and how 3ds Max works. Without it, agents will guess wrong on material workflows, controller paths, and plugin APIs. The installer builds and deploys it automatically. 

However Anthropic models seem to REALLY like using maxscript instead of using the native tooling unlike Codex which uses the right tool most of the time.

If you need to rebuild manually:
```powershell
python scripts/build_skill.py
```

## Safe mode

Three layers of substring blocklist, kept in lockstep:

- **Python (`src/helpers/safe_mode.py`)** — applied to
  `execute_maxscript` before the script reaches the bridge. Disable
  per-process with `MCP_SAFE_MODE=false`.
- **Native bridge (`native/src/command_dispatcher.cpp`)** — applied
  to every MAXScript dispatch the named pipe receives.
- **MAXScript TCP fallback (`maxscript/mcp_server.ms`)** — same
  list, re-applied at the legacy 2024/2025 entry point.

Both bridge layers read from a shared config:

```
%LOCALAPPDATA%\3dsmax-mcp\mcp_config.ini
```

```ini
[mcp]
safe_mode = true
```

When enabled (default), scripts containing any of these case-insensitive
fragments are rejected: `DOSCommand`, `HiddenDOSCommand`, `ShellLaunch`,
`deleteFile`, `createFile`, `python.Execute`, `dotNetClass`,
`dotNetObject`, `dotNetMethod`, `loadAssembly`, `fileIn`,
`registerOLEInterface`, `decodeBase64`, `encodeBase64`,
and `execute(` / `execute (`.

### Scope — read this

`safe_mode` is an **accident preventer**, not a sandbox. It is a
case-insensitive substring blocklist, so a determined author (or a
sufficiently clever LLM) can bypass it with string concatenation,
runtime symbol lookup, etc. It catches the obvious shapes — LLM
hallucinates `deleteFile` → rejected — not an adversarial MaxScript
author.

What it **doesn't** cover:

- Native C++ handlers run unfiltered: `delete_objects`,
  `manage_scene` (reset/new/open), `render_scene`,
  `merge_from_file`, `write_osl_shader`, `capture_*` (disk writes).
  Filesystem-touching tools have separate guards via the path policy
  below.
- The `\\.\pipe\3dsmax-mcp` named pipe uses the default ACL — any
  process running as your user can open it and send commands. Fine
  on a single-user artist machine; if you need multi-user isolation,
  gate on `GetNamedPipeClientProcessId`.

## Path policy

Tools that take filesystem paths (`render_scene`, `merge_from_file`,
`inspect_max_file`, `batch_file_info`, `search_max_files`) are gated
by `src/helpers/paths.py` so an LLM-generated path cannot reach into
the user's profile or system folders.

| Env var | Effect |
|---|---|
| `MCP_PROJECT_ROOTS` | Semicolon-/colon-separated absolute roots that paths must live under. When set, this is the only allow-list. |
| `MCP_ALLOW_ANY_PATH=true` | Disable the allow-list (still blocks SSH/AWS/credential paths). |
| _(neither set)_ | Default allow-list: `~/Documents`, `~/Desktop`, `~/Downloads`, `~/3dsMax`, `~/Max`, system temp. |

Paths under SSH/AWS/GnuPG/credential folders, `System32`, etc. are
**always** blocked, even with `MCP_ALLOW_ANY_PATH`.

## Tools

Default full profile: 116 tools across scene management, objects, materials, modifiers, controllers, wiring, viewport capture, file access, plugin introspection, tyFlow, Forest Pack, RailClone, Data Channel, and more. Core profile: 79 tools with concise descriptions.

| Category | Tools | Transport |
|----------|-------|-----------|
| Scene reads | `get_scene_info`, `get_selection`, `get_scene_snapshot`, `get_selection_snapshot`, `get_scene_delta`, `get_hierarchy` | C++ |
| Objects | `create_object`, `delete_objects`, `transform_object`, `clone_objects`, `select_objects`, `set_object_property`, `set_visibility`, `set_parent` | C++/Hybrid |
| Inspection | `inspect_object`, `inspect_properties`, `introspect_class`, `introspect_instance`, `walk_references`, `learn_scene_patterns`, `map_class_relationships` | C++ |
| Materials | `assign_material`, `set_material_properties`, `get_material_slots`, `create_texture_map`, `palette_laydown`, `write_osl_shader`, `create_shell_material`, `replace_material` | Hybrid |
| Modifiers | `add_modifier`, `remove_modifier`, `set_modifier_state`, `collapse_modifier_stack`, `batch_modify` | Hybrid |
| Controllers | `assign_controller`, `inspect_controller`, `inspect_track_view`, `set_controller_props`, `add_controller_target` | Hybrid |
| Wiring | `wire_params`, `unwire_params`, `get_wired_params`, `list_wireable_params` | Hybrid |
| Viewport | `capture_viewport`, `capture_multi_view`, `capture_screen`, `render_scene` | C++ |
| Organization | `manage_layers`, `manage_groups`, `manage_selection_sets`, `manage_scene` | C++ |
| File access | `inspect_max_file`, `merge_from_file`, `search_max_files`, `batch_file_info` | C++ |
| Plugins | `discover_plugin_classes`, `introspect_class`, `introspect_instance`, `get_plugin_capabilities` | C++ |
| Scene events | `watch_scene`, `get_scene_delta` | C++ |
| tyFlow | `create_tyflow`, `get_tyflow_info`, `modify_tyflow_operator`, `set_tyflow_shape`, `reset_tyflow_simulation` | MAXScript |
| Forest Pack | `scatter_forest_pack` | MAXScript |
| Data Channel | `add_data_channel`, `inspect_data_channel`, `set_data_channel_operator` | MAXScript |
| Scripting | `execute_maxscript` | Pipe |

## Audit log

Every MCP tool call emits one JSON line to a per-day file so a studio
SIEM can answer "what tool ran on this host, when, and how big was the
result?" without retaining any payload. The schema is:

```json
{"ts":"2026-05-22T03:14:15.926Z","tool":"render_scene","ok":true,
 "elapsed_ms":42.7,"arg_bytes":128,"result_bytes":15872,
 "error_type":null,"transport":"native"}
```

Default location:

- Windows — `%LOCALAPPDATA%\3dsmax-mcp\logs\tool_calls-YYYY-MM-DD.jsonl`
- macOS / Linux — `$XDG_STATE_HOME/3dsmax-mcp/logs/` (or
  `~/.local/state/3dsmax-mcp/logs/`)

Knobs:

| Env var | Effect |
|---|---|
| `MCP_AUDIT_DIR` | Override the log directory (point at a SIEM-watched path). |
| `MCP_DISABLE_AUDIT=true` | Opt out (for power users only — IT should not). |

The audit log records **sizes only**. Argument values, result bodies,
API keys, and chat prompts never reach the log file. The LLM-call
audit (see `GHOTI-248`) covers the chat egress story separately. Audit
write failures degrade silently — a bad log directory cannot break
tool calls.

## Optional: in-Max chat (off by default)

The bridge can host an LLM chat window inside 3ds Max with direct
scene-editing tools. This routes scene contents through Anthropic's
Messages API and is **disabled by default**. Enable on machines you
have explicitly cleared for third-party LLM egress:

1. Set `MCP_ENABLE_CHAT=1` in the environment the MCP server runs in.
   - The Python MCP server reads it at tool registration to decide
     whether to expose `send_to_chat` / `chat_status` / `chat_reload`
     / `chat_clear`.
   - The C++ GUP plugin reads it at Max startup to decide whether to
     initialise the chat UI, the LLM client, and the `MCP Chat`
     toolbar macroscript.
2. Put `ANTHROPIC_API_KEY=sk-ant-...` in
   `%LOCALAPPDATA%\3dsmax-mcp\.env` (or as a real env var).
3. Restart 3ds Max.

Default model is `claude-sonnet-4-6` for cost control. Override in
`mcp_config.ini` `[llm] model`. There is **no** OpenAI / OpenRouter
fallback in this fork — the client talks to
`https://api.anthropic.com/v1/messages` directly.

When `MCP_ENABLE_CHAT` is unset (the default), the chat surface is
fully cold: no Python tools, no C++ chat window, no toolbar button,
no LLM HTTP traffic.

- **Token controls (when enabled):** standalone chat defaults to
  `prompt_mode=compact`, `tool_profile=full`,
  `include_scene_snapshot=true`, `max_scene_roots=25`,
  `max_prompt_chars=12000`, `max_tool_result_chars=12000`,
  `max_history_tool_chars=1800`, `max_tool_summary_chars=600`,
  `max_display_tool_chars=600`, `max_tool_loops=4`. Use
  `tool_profile=core` for a smaller per-turn tool surface.
- **Slash commands:** `/reload`, `/clear`, `/help`.

## Building from source (native bridge)

**This fork ships no prebuilt `.gup` binaries** — they were removed
because the upstream binaries embed the OpenAI-compat / OpenRouter
chat client. Rebuild from source before deploying.

**Max 2024 / 2025 / 2026** — Visual Studio 2022 (v143), C++17, CMake 3.20+
**Max 2027+** — Visual Studio 2022 (v143), C++20, CMake 3.20+

The fastest path is `build_all.bat`, which builds every version whose
SDK is installed and stages each `.gup` into `native/bin/` with the
suffix that `install.py` expects:

```powershell
cd native
build_all.bat              # detects installed SDKs, builds each
build_all.bat 2026 2027    # build only the versions you name
```

Output layout:

```
native/bin/mcp_bridge.gup        ← Max 2026 (the "default" target)
native/bin/mcp_bridge_2024.gup
native/bin/mcp_bridge_2025.gup
native/bin/mcp_bridge_2027.gup
```

`install.py` reads the artist's installed Max version and copies the
matching `.gup` to `C:\Program Files\Autodesk\3ds Max <version>\plugins\`.
If no matching binary is staged, install fails loudly with a build
command — the 2026 binary is **not** ABI-compatible with other
versions and we refuse to silently substitute it.

For a single-version build:

```powershell
cd native
cmake -B build -G "Visual Studio 17 2022" -A x64 -DMAX_VERSION=2026
cmake --build build --config Release
```
