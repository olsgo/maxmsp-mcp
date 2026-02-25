# MaxMSP MCP Server

This project provides MCP tools for programmatic Max/MSP patch manipulation.

## Critical Rules

**Run `/maxmsp` skill before creating or modifying patches** - it contains all placement rules, object gotchas, and tool usage guidelines that MUST be followed.

### Quick Reminders (details in skill)

- **CONSIDER SUBPATCHERS** for new functionality!
- **NO OVERLAP**: Always call `get_avoid_rect_position()` before placing objects
- **Message boxes**: Use numbers `[200, 0, 50]` not strings `["200", "0", "50"]`
- **Auto-sizing**: Objects & comments auto-size; messages fixed 70px; UI objects keep defaults

### Required Flags

- **Math/pack/unpack**: JSON strips `.0` from numbers. Use STRING args to preserve floats: `["0", "127", "0", "25."]`. Use `["f", "f", "f"]` for unpack. Set `int_mode=True` to explicitly allow integers. Exception: `scale` with output range ≤ 2 auto-detects float intent.
- **dial**: Use `dial` with `@size` attribute instead of `live.dial` (set `use_live_dial=True` to bypass)
- **trigger/t**: Set `trigger_rtl=True` - fires right-to-left (`[t b f]` sends `f` first)
- **coll**: Always include `@embed 1` to persist data on save

## MCP Tools

Key tools for object manipulation:
- `get_avoid_rect_position()` - Get bounding box before placing
- `add_max_object()` - Create object (auto-fits width)
- `recreate_with_args()` - Change creation-time args, preserving connections
- `move_object()` - Reposition object
- `autofit_existing()` - Apply auto-fit to existing object

Managed runtime/file workflow tools:
- `ensure_max_available()` / `bridge_status()` - no-manual-setup bridge bring-up and health
- `load_patch_from_path()` / `save_patch_to_path()` - inspect/import/export `.maxpat` directly from filesystem
- `get_patch_context()` / `dry_run_plan()` - higher-context introspection and preflight validation
- `create_checkpoint()` / `restore_checkpoint()` / `run_patch_transaction()` - rollback-safe edit execution
- `list_max_system_sessions()` / `close_max_system_sessions()` - inspect and close system-wide Max sessions/processes
- `cleanup_max_hygiene()` / `get_hygiene_report()` / `set_hygiene_policy()` - automated hygiene/GC and reporting

## Architecture

- `server.py` - Python FastMCP server with Socket.IO
- `MaxMSP_Agent/mcp_host.maxpat` - Managed bridge patch auto-opened in managed mode
- `MaxMSP_Agent/max_mcp.js` - Main Max-side JavaScript handler
- `MaxMSP_Agent/max_mcp_v8_add_on.js` - V8 JavaScript with `obj.boxtext` access

**After code changes**: Reload js objects in Max (double-click to open editor, then close). Managed mode autostarts `node.script`.

## Runtime Defaults

- Managed mode is default (`MAXMCP_MANAGED_MODE=1`) and opens `MaxMSP_Agent/mcp_host.maxpat` automatically.
- Installer writes/preserves `MAXMCP_AUTH_TOKEN` so bridge auth works without manual setup.
- Optional file-scope guardrails:
  - `MAXMCP_ENFORCE_PATCH_ROOTS=1`
  - `MAXMCP_ALLOWED_PATCH_ROOTS=/abs/path/a:/abs/path/b`
- Hygiene defaults:
  - `MAXMCP_HYGIENE_AUTO_CLEANUP=1`
  - `MAXMCP_HYGIENE_SCOPE=all_max_instances`
  - `MAXMCP_HYGIENE_MODE=aggressive`
  - `MAXMCP_HYGIENE_STALE_SECONDS=1800`
