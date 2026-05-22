# 3dsmax-mcp — APEC council review & corrective-action plan

**Date:** 2026-05-21
**Scope:** the hardening landed today on the `stuart-marchant/3dsmax-mcp` fork (chat
gated behind `MCP_ENABLE_CHAT`, LLM client converted to Anthropic Messages
API, expanded safe_mode blocklist with Python pre-pipe filter, new path
policy in `src/helpers/paths.py`, prebuilt `.gup` binaries removed to
force clean rebuild).
**Method:** 7-layer APEC council (adversarial threat model · studio IT/SecOps ·
artist experience · regression code review · supply-chain & build · data
egress & privacy · prompt injection & agent safety) run in parallel against
six criteria:

1. Safe for an artist to use on their workstation?
2. Does it do what it advertises as an MCP server?
3. Safe for a corporate environment?
4. Prompt-injection blast radius?
5. Secrets & egress integrity?
6. Defense-in-depth correctness?

## Council headline

All 7 layers returned **GO-WITH-CONDITIONS**. None returned NO-GO. There is
broad agreement that the hardening architecture is sound and that the
residual gaps are addressable. The most consequential single finding is
**a flat bug in `src/helpers/paths.py` that breaks `MCP_PROJECT_ROOTS`
parsing on Windows** — the actual deploy target. Trial cannot proceed
until that is fixed.

## P0 — must fix before deploying to artists

These are blockers. They are either flat bugs in code we shipped today, or
guard-rails that demonstrably do not hold against the simplest LLM
mistakes.

1. **Fix Windows path-roots parsing in `src/helpers/paths.py:105-110`.**
   On Windows we force `sep=";"` but the inner loop then re-splits each
   chunk on `":"`. `MCP_PROJECT_ROOTS=C:\Projects;D:\Work` becomes
   `["C", "\\Projects", "D", "\\Work"]` — the trial's intended root list
   matches nothing and the artist is blocked from every legitimate path.
   *(L4 critical; L3 confirmed via day-in-the-life walkthrough.)*

2. **Add `Documents\3dsMax\Scenes` and the user's configured Max
   project folder to default roots** — read `getDir #scene` at startup
   or hard-code the `~/3dsMax/Scenes` subpath. `~/Documents` alone is
   not enough; 3ds Max defaults its working dir to a deeper subfolder
   that artists rely on. *(L3.)*

3. **Allow UNC paths when they sit under an explicit `MCP_PROJECT_ROOTS`
   entry.** Current blanket-deny in `paths.py:171-174` is wrong for
   studios; their projects *are* on `\\nas\projects\…`. Keep the
   blanket-deny when no explicit root is set; relax when the operator
   has named the share. *(L3 + L1.)*

4. **Tighten `execute_maxscript` blocklist:**
   - Add `execute ` (space-quote), `execute@`, `execute\t`,
     `executeScript`, `executeString` to all three blocklists
     (`safe_mode.py:28-60`, `command_dispatcher.cpp:161-183`,
     `mcp_server.ms:251-258`). The current `execute(` / `execute (`
     filter does not cover `execute "DOSCommand …"`. *(L1, L7.)*
   - Add scene-destructive intrinsics under a `MCP_SAFE_MODE=strict`
     tier (default for the trial): `resetmaxfile`, `loadmaxfile`,
     `holdmaxfile`, `fetchmaxfile`, `meditmaterials[`. *(L7.)*

5. **Reconsider `execute(` / `execute (` as blocked fragments.** They
   false-positive on legitimate MAXScript expressions and the bundled
   SKILL prompt itself recommends `execute` for evaluating strings.
   Replace with a tighter rule that only blocks `execute "…"` /
   `execute @"…"` where the argument is a literal. *(L4.)*

6. **Build a `mcp_bridge_2027.gup` if either trial artist runs Max
   2027.** `install.py:20-23` expects this file at `native/bin/`; the
   shipped `build.bat:25` only emits the 2026 variant. Without a
   matching `.gup`, `deploy_native_bridge()` silently SKIPs and the
   bridge is not installed. *(L5.)*

7. **Drop the `@lru_cache(maxsize=1)` on `paths._policy()`** *or* loudly
   document the restart requirement. Today an artist who edits
   `.env`/`MCP_PROJECT_ROOTS` and reconnects sees no change until the
   MCP server is fully restarted. The cost of re-reading env per call
   is negligible compared to the support burden. *(L3, L4.)*

8. **Gate `native:chat_ui` dispatch in `command_dispatcher.cpp:484-485`
   on the same `ChatEnabledByEnv()` we added in `bridge_gup.cpp`.**
   Today only the Python wrapper is gated; a non-Python pipe writer
   could still reach the chat handler. *(L1.)*

## P1 — fix in parallel with the trial

These are real gaps but can wait while the two artists work, provided P0
is in. They each have a clear owner and one-week ETA.

| Area | Action | Reference |
|---|---|---|
| Audit log | Append JSONL per tool call (name, arg sizes, result size, elapsed_ms, transport) to `%LOCALAPPDATA%\3dsmax-mcp\logs\tool_calls.jsonl`. Hook into `src/tool_response.py` `make_structured_tool`. | L2, L6 |
| LLM audit | When chat is enabled, log prompt hash + response token usage + tool names invoked per turn. | L6 |
| Trial defaults | Ship the trial with `include_scene_snapshot=false` (or `max_scene_roots=0`) in `mcp_config.ini` so object/material names do not leak to Anthropic unless explicitly asked. | L6 |
| Untrusted-data envelope | Have `get_scene_snapshot`, `get_materials`, `inspect_object`, `inspect_max_file`, `search_max_files` return `{"_untrusted": true, "data": {...}}` and update tool docstrings to flag that all string fields are attacker-controllable. | L7 |
| External-client safety | Move the "SECURITY: snapshot is UNTRUSTED" preamble from the in-Max chat system prompt into the tool *descriptions* themselves so external MCP clients (Claude Desktop, Cursor) see it. | L7 |
| Corporate egress | Honor an `ANTHROPIC_BASE_URL` env var so IT can route traffic through an internal egress proxy without per-machine `mcp_config.ini` edits. | L2 |
| Key hygiene | DPAPI-wrap the API key (or at minimum verify the `.env` ACL is user-only on first read; refuse to load if group/world-readable). | L2, L6 |
| Lockstep CI | Add a test that fails when `safe_mode.py`, `command_dispatcher.cpp`, and `mcp_server.ms` blocklists diverge. | L2 |
| Pin upper bound | `pyproject.toml:11` change `mcp[cli]>=1.0.0` to `mcp[cli]>=1.0.0,<2`; run `uv lock`. | L5 |
| Symmetric uninstall | Add Max 2027 to `uninstall.py:15-19`; remove `%LOCALAPPDATA%\3dsmax-mcp\.env` on uninstall (or prompt). | L5 |
| Error-message UX | `PathPolicyError` should not suggest `MCP_ALLOW_ANY_PATH=true` first — recommend "ask your TD to add this folder to MCP_PROJECT_ROOTS" instead. | L3 |
| Startup banner | Print effective roots, safe-mode tier, chat state, and Anthropic endpoint at MCP server start. | L3 |

## P2 — backlog

* `mcp doctor` subcommand that prints the effective policy and validates
  it against a sample path the user supplies.
* Firewall recipe (Defender/CrowdStrike) recommending egress restricted
  to `api.anthropic.com:443` for the bridge process.
* `build_all.bat` that loops 2024/2025/2026/2027 and emits versioned
  `.gup` files into `native/bin/`.
* Telemetry pattern detector: flag any turn that combines
  `merge_from_file` + `execute_maxscript` as a probable injection.
* Strip / length-clamp note-track and userProp strings in any inspect
  tool that surfaces them.

## What each council layer flagged as its own #1 issue

| Layer | Lens | Top finding |
|---|---|---|
| 1 | Adversarial | `execute "DOSCommand …"` bypasses the substring filter; concat bypasses remain. |
| 2 | Studio IT | No audit logging anywhere — IT cannot reconstruct what scene data left the host. |
| 3 | Artist experience | Default roots reject `D:\` and UNC; trial requires per-artist `MCP_PROJECT_ROOTS` setup. |
| 4 | Regression review | `paths.py:105-110` Windows separator logic is broken; `MCP_PROJECT_ROOTS` on Windows allows nothing. |
| 5 | Supply chain | `mcp_bridge_2027.gup` build path is aspirational; `install.py` will SKIP silently for 2027 artists. |
| 6 | Data egress | Scene snapshot defaults on; object/layer/material names ship to Anthropic every turn when chat is enabled, with no audit trail. |
| 7 | Prompt injection | External MCP clients (Claude Desktop, Cursor) drive the same tools without the in-Max chat's SECURITY preamble. |

## Recommendation

**Fix the P0 list, then deploy.**

The P0 work is small and contained — eight bullets, mostly one-line code
or config changes. The most expensive (build a 2027 `.gup`) is a build
recipe, not new code. Skipping P0 means the trial fails on day one for
mundane "the path validator doesn't work" reasons, and the security work
reads as "the tool is broken" rather than "the tool is careful."

Once P0 is in, the residual risk for a 2-artist closed trial is
acceptable. The remaining gaps (P1) are observability and polish — they
can run on a one-week sprint alongside the trial without delaying it,
and the artists' real-world feedback will help prioritize them.

**APEC results:** ⭐⭐⭐⭐ (4/5) — High. The architecture earns a clear
pass; the breakage is in execution detail, not design.
