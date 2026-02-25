# MaxMSP-MCP Server (Extended Fork)

This project uses the [Model Context Protocol](https://modelcontextprotocol.io/introduction) (MCP) to let LLMs directly understand and generate Max patches.

> **Fork Notice**: This is an extended fork of the original [MaxMSP-MCP-Server](https://github.com/tiianhk/MaxMSP-MCP-Server) by Haokun Tian and Shuoyang Zheng. See [Acknowledgements](#acknowledgements) for details.

## What's New in This Fork

This fork significantly extends the original with new tools, safety features, and Claude Code integration:

### New MCP Tools (+11)

| Tool | Description |
|------|-------------|
| `create_subpatcher` | Create a new `p` (subpatcher) object |
| `enter_subpatcher` | Navigate into a subpatcher context |
| `exit_subpatcher` | Return to parent patcher |
| `get_patcher_context` | Get current depth and navigation path |
| `add_subpatcher_io` | Add inlet/outlet objects inside subpatchers |
| `get_object_connections` | Query all connections for an object |
| `recreate_with_args` | Change creation-time arguments, preserving connections |
| `move_object` | Reposition an object |
| `autofit_existing` | Apply auto-sizing to existing objects |
| `check_signal_safety` | Analyze patch for dangerous signal patterns |
| `encapsulate` | Encapsulate selected objects into a subpatcher |

### Safety & Validation Features

- **Float enforcement**: Math objects (`+`, `-`, `*`, `/`, `%`, `pow`, `scale`) and pack/unpack objects require float arguments. Use STRING args to preserve floats (JSON strips `.0`): `["0", "127", "0", "25."]`. Use `int_mode=True` to explicitly allow integers. Exception: `scale` with output range ≤ 2 auto-detects float intent.
- **dial range enforcement**: Rejects `live.dial` (suggests `dial` with inline attributes); requires `@size` on `dial` objects; rejects `@size > 255` (unusable UI - use `extend=True` to bypass)
- **trigger/t acknowledgment**: Requires `trigger_rtl=True` flag to confirm understanding that outlets fire right-to-left
- **coll embed enforcement**: Requires `@embed 1` in args to ensure data persists on save
- **line~ message validation**: Rejects messages with odd numeric count (likely malformed target-time pairs)
- **Object validation**: Rejects invalid objects (e.g., `times~` → suggests `*~`)
- **Argument validation**: Enforces minimum arguments for complex objects (e.g., `comb~` requires 5 args)
- **Parameter range checks**: Catches common mistakes like svf~ Q >= 1 or onepole~ frequency < 10 Hz
- **Large patch warnings**: Alerts when root patcher exceeds 80 objects
- **Signal safety analysis**: Detects feedback loops, high gain, unsafe comb~ feedback, and missing limiters before `dac~`

### Quality of Life Improvements

- **Auto-sizing**: Objects and comments automatically fit their content
- **Increased timeouts**: Better handling of large patchers (5s vs 2s)
- **Subpatcher support**: Full navigation and creation within nested patchers
- **Alias preservation**: Encapsulate preserves user-facing names (`*~` not `times~`, `t` not `trigger`)

---

## Demo Videos

### Understand: LLM Explaining a Max Patch

![img](./assets/understand.gif)

[Video link](https://www.youtube.com/watch?v=YKXqS66zrec). Acknowledgement: the patch being explained is from [MaxMSP_TeachingSketches](https://github.com/jeffThompson/MaxMSP_TeachingSketches/blob/master/02_MSP/07%20Ring%20Modulation.maxpat).

### Generate: LLM Making an FM Synth

![img](./assets/generate.gif)

[Full video](https://www.youtube.com/watch?v=Ns89YuE5-to) with audio.

---

## Installation

### Prerequisites

- Python 3.8 or newer
- [uv package manager](https://github.com/astral-sh/uv)
- Max 9 or newer (requires JavaScript V8 engine)

### Installing the MCP Server

1. **Install uv:**
```bash
# macOS/Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows:
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

2. **Clone and set up:**
```bash
git clone https://github.com/tiianhk/MaxMSP-MCP-Server.git
cd MaxMSP-MCP-Server
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

3. **Connect to your MCP client:**

**For Claude Code (recommended):**

Add to your Claude Code MCP settings (`~/.claude/settings.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "maxmsp": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/MaxMSP-MCP-Server",
        "run",
        "server.py"
      ]
    }
  }
}
```

**For Claude Desktop, Cursor, or Codex:**

```
python install.py --client claude
# or
python install.py --client cursor
# or
python install.py --client codex
```

Codex writes to `~/.codex/config.toml` under `[mcp_servers.maxmsp]`.
The installer auto-generates `MAXMCP_AUTH_TOKEN` on first run and preserves it on later runs.
Token resolution precedence at runtime is: `MAXMCP_AUTH_TOKEN` (env) → `MAXMCP_AUTH_TOKEN_FILE` → unset.

### Managed Mode (No Manual Max Setup)

Managed mode is enabled by default.

When the MCP server starts, it will automatically:
1. Ensure Node dependencies are installed in `MaxMSP_Agent/`
2. Launch Max (`/Applications/Max.app` by default)
3. Open `MaxMSP_Agent/mcp_host.maxpat`
4. Provision per-session `active.maxpat` and `scratch.maxpat` under `target/maxmcp/sessions/<session-id>/`
5. Attach the bridge to a per-session workspace target (`active` by default)
6. Reconnect the bridge if Max/bridge disconnects

Useful tools:
- `ensure_max_available()` - force readiness check and launch/reconnect if needed
- `bridge_status()` - inspect bridge health
- `get_bridge_metrics()` - latency/action/queue telemetry (optional recent events)
- `recover_bridge()` - force recovery sequence
- `list_patch_targets()` - list managed targets (`host`, `active`, `scratch`)
- `set_patch_target()` - switch between per-session `active`/`scratch` workspaces or `host`
- `sync_patch_twin()` / `get_patch_drift()` - twin synchronization and drift checks
- `create_checkpoint()` / `restore_checkpoint()` - snapshot + rollback
- `run_patch_transaction()` - multi-step execution with automatic rollback on failure
- `validate_patch_file()` - static parse/shape validation for `.maxpat`/JSON files
- `load_patch_from_path()` - direct file import into `active`/`scratch` (replace/merge/fail-if-not-empty)
- `save_patch_to_path()` - export workspace topology to `.maxpat`/JSON file
- `list_max_system_sessions()` - system-wide Max process + patch visibility
- `close_max_system_sessions()` - close stale/managed/custom Max sessions
- `cleanup_max_hygiene()` - aggressive stale-process + stale-session GC
- `get_hygiene_report()` / `set_hygiene_policy()` - hygiene audit + runtime policy control

If your Max app is in a non-default path, set:
- `MAXMCP_MAX_APP=/path/to/Max.app`

Reliability/security knobs:
- `MAXMCP_STRICT_V2_ENFORCEMENT=1` require protocol-v2 response envelopes
- `MAXMCP_STRICT_CAPABILITY_GATING=1` block actions not advertised by bridge capabilities
- `MAXMCP_MUTATION_MAX_INFLIGHT=4` / `MAXMCP_MUTATION_MAX_QUEUE=64` mutation concurrency + queue backpressure
- `MAXMCP_MUTATION_QUEUE_WAIT_TIMEOUT_SECONDS=15` max wait to acquire mutation slot
- `MAXMCP_AUTH_TOKEN=...` shared token attached by Python bridge and validated in `max_mcp_node.js`
- `MAXMCP_AUTH_TOKEN_FILE=~/.maxmsp-mcp/auth_token` fallback token file used when env token is unset
- `MAXMCP_REQUIRE_HANDSHAKE_AUTH=1` require Socket.IO handshake auth (`auth.token` / `x-maxmcp-token`)
- `MAXMCP_ALLOW_REMOTE=0` keep Node bridge localhost-only (set to `1` to allow remote clients)
- `MAXMCP_ENFORCE_PATCH_ROOTS=1` enforce patch path allowlisting
- `MAXMCP_ALLOWED_PATCH_ROOTS=/abs/path/a:/abs/path/b` allowlisted roots for patch file import/export
- `MAXMCP_METRICS_LOG_INTERVAL_SECONDS=30` periodic structured metrics log interval
- `MAXMCP_ALERT_FAILURE_RATE=0.10` rolling failure-rate alert threshold
- `MAXMCP_ALERT_P95_MS=1500` rolling p95 latency alert threshold (ms)
- `MAXMCP_ALERT_QUEUE_DEPTH=0.80` queue saturation alert threshold (fraction)
- `MAXMCP_ALERT_WINDOW_SECONDS=300` rolling alert window
- `MAXMCP_HYGIENE_AUTO_CLEANUP=1` enable background hygiene cleanup loop
- `MAXMCP_HYGIENE_SCOPE=all_max_instances` cleanup scope (`all_max_instances` or `managed_only`)
- `MAXMCP_HYGIENE_MODE=aggressive` default cleanup mode (`aggressive` or `preview`)
- `MAXMCP_HYGIENE_STALE_SECONDS=1800` stale threshold (30m default)
- `MAXMCP_HYGIENE_STARTUP_SWEEP=1` run startup cleanup once when runtime becomes ready
- `MAXMCP_HYGIENE_MAX_KILLS_PER_SWEEP=50` cap process kills per sweep

Patch file import/export tools return structured failures when bridge policy rejects an operation:
- capability gating (`PRECONDITION_FAILED`)
- auth failures (`UNAUTHORIZED`)
- queue/backpressure (`OVERLOADED`)

If root enforcement is enabled and no allowlist is provided, defaults are:
- repository root
- session workspace directory (`target/maxmcp/sessions/<session-id>/`)

### One-Command Smoke Test

Run this from repo root:

```bash
.venv/bin/python scripts/smoke_managed_runtime.py
```

The smoke test validates:
- managed runtime startup/readiness
- bridge health ping
- mutation roundtrip (`add_object` + `remove_object`)

Optional live E2E unittest (runs only when explicitly enabled):

```bash
MAXMCP_RUN_LIVE_E2E=1 ./.venv/bin/python -m unittest -v tests/test_live_bridge_e2e.py
```

### Local Validation Commands

Fast local checks (compile/parse + unit/protocol/soak):

```bash
./.venv/bin/python scripts/check_fast.py
```

Live bridge checks (smoke + live E2E + soak):

```bash
./.venv/bin/python scripts/check_live.py
```

Rotate auth token and sync local client config (default client: codex):

```bash
./.venv/bin/python scripts/rotate_auth_token.py --clients codex
```

---

## Architecture

```
┌─────────────────┐     Socket.IO      ┌─────────────────┐
│   Claude Code   │ ←───────────────→  │    server.py    │
│  (MCP Client)   │     (port 5002)    │  (FastMCP/Python)│
└─────────────────┘                    └────────┬────────┘
                                                │
                                       ┌────────▼────────┐
                                       │ max_mcp_node.js │
                                       │   (Node.js)     │
                                       └────────┬────────┘
                                                │
                              ┌─────────────────┴─────────────────┐
                              │                                   │
                     ┌────────▼────────┐              ┌───────────▼───────────┐
                     │   max_mcp.js    │              │ max_mcp_v8_add_on.js  │
                     │  (Max js object)│              │   (Max v8 runtime)    │
                     └─────────────────┘              └───────────────────────┘
```

- **server.py** - Python FastMCP server with Socket.IO, validation, and tool definitions
- **mcp_host.maxpat** - Managed bridge patch auto-opened by runtime manager
- **max_mcp_node.js** - Node.js bridge running inside Max's `node.script`
- **max_mcp.js** - Main Max-side JavaScript handler for most operations
- **max_mcp_v8_add_on.js** - V8 JavaScript with `boxtext` access for encapsulation

---

## MCP Tools Reference

### Object Creation & Manipulation

| Tool | Description |
|------|-------------|
| `add_max_object(position, obj_type, varname, args)` | Create an object |
| `remove_max_object(varname)` | Delete an object |
| `connect_max_objects(src, outlet, dst, inlet)` | Connect two objects |
| `disconnect_max_objects(src, outlet, dst, inlet)` | Disconnect objects |
| `move_object(varname, x, y)` | Reposition an object |
| `recreate_with_args(varname, new_args)` | Change creation-time args |
| `autofit_existing(varname)` | Auto-size existing object |

### Object Properties

| Tool | Description |
|------|-------------|
| `set_object_attribute(varname, attr, value)` | Set an attribute |
| `set_message_text(varname, text_list)` | Set message box content |
| `set_number(varname, num)` | Set number box/slider value |
| `send_bang_to_object(varname)` | Send a bang |
| `send_messages_to_object(varname, message)` | Send message list |

### Query Tools

| Tool | Description |
|------|-------------|
| `ensure_max_available()` | Ensure Max and bridge patch are available |
| `bridge_status(verbose?)` | Runtime/bridge health summary |
| `recover_bridge()` | Relaunch/reconnect bridge runtime |
| `list_max_system_sessions(include_windows?, include_runtime_state?)` | Inventory Max processes, open docs, and managed sessions |
| `close_max_system_sessions(target?, pids?, force?, dry_run?, max_count?)` | Close selected Max sessions/processes |
| `cleanup_max_hygiene(mode?, include_processes?, include_session_dirs?, dry_run?)` | Execute stale process/session cleanup |
| `get_hygiene_report(limit?)` | Show recent hygiene actions and policy |
| `set_hygiene_policy(...)` | Update in-memory hygiene policy |
| `list_patch_targets()` | Show logical patch targets |
| `get_objects_in_patch()` | Get all objects and connections |
| `get_objects_in_selected()` | Get selected objects |
| `get_object_attributes(varname)` | Get object's attributes |
| `get_object_connections(varname)` | Get object's connections |
| `get_avoid_rect_position()` | Get bounding box for placement |
| `list_all_objects()` | List available Max objects |
| `search_objects(query, package?, limit?)` | Search MaxPyLang object catalog |
| `get_object_schema(name)` | Get MaxPyLang object args/attrs/IO schema |
| `get_object_doc(name)` | Get Max documentation |
| `sync_patch_twin(reason?)` | Sync in-memory topology twin |
| `get_patch_drift(auto_resync?)` | Detect topology drift vs twin baseline |
| `create_checkpoint(label?)` | Capture rollback checkpoint |
| `list_checkpoints()` | List stored checkpoints |
| `restore_checkpoint(id)` | Restore a checkpoint snapshot |
| `run_patch_transaction(steps, ...)` | Execute steps atomically with rollback |

### Subpatcher Navigation

| Tool | Description |
|------|-------------|
| `create_subpatcher(position, varname, name)` | Create a `p` object |
| `enter_subpatcher(varname)` | Navigate into subpatcher |
| `exit_subpatcher()` | Return to parent |
| `get_patcher_context()` | Get current depth/path |
| `add_subpatcher_io(position, io_type, varname)` | Add inlet/outlet |

### Safety & Organization

| Tool | Description |
|------|-------------|
| `check_signal_safety()` | Analyze for dangerous patterns |
| `encapsulate(varnames, name, varname)` | Encapsulate objects |
| `dry_run_plan(steps, engine?)` | Validate planned edits (`engine="basic"` or `engine="maxpy"`) |

---


## Development

After making code changes:

1. Reload js objects in Max (double-click to open editor, then close)
2. Restart node.script (`script stop`, then `script start`)

---

## Acknowledgements

This fork is based on the original [MaxMSP-MCP-Server](https://github.com/tiianhk/MaxMSP-MCP-Server) created by **Haokun Tian** and **Shuoyang Zheng**.

The original project is licensed under the MIT License. See [LICENSE](./LICENSE) for details.

**Original repository:** https://github.com/tiianhk/MaxMSP-MCP-Server

---

## Disclaimer

This is a third-party implementation and not made by Cycling '74.
