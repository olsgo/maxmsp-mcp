#!/usr/bin/env python3
"""Rotate MAXMCP auth token and synchronize local client config entries."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

import install


def _write_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(token + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def _update_json_client(
    client: str,
    config_path: Path,
    current_dir: str,
    token: str,
    token_file: str,
    create_entry: bool,
) -> dict:
    if not config_path.exists() and not create_entry:
        return {"client": client, "updated": False, "reason": "config_missing"}

    config_data = install.load_json(config_path)
    mcp_name = "mcpServers" if client != "vscode" else "servers"
    if mcp_name not in config_data or not isinstance(config_data[mcp_name], dict):
        config_data[mcp_name] = {}

    existing = config_data[mcp_name].get("MaxMSPMCP")
    if not isinstance(existing, dict):
        if not create_entry:
            return {"client": client, "updated": False, "reason": "entry_missing"}
        common_env = install.build_common_env(
            current_dir,
            token,
            auth_token_file=token_file,
        )
        config_data[mcp_name]["MaxMSPMCP"] = {
            "command": "mcp",
            "args": ["run", os.path.join(current_dir, "server.py")],
            "env": {
                "PATH": os.path.join(current_dir, ".venv/bin"),
                "VIRTUAL_ENV": os.path.join(current_dir, ".venv"),
                **common_env,
            },
        }
    else:
        env = existing.get("env")
        if not isinstance(env, dict):
            env = {}
            existing["env"] = env
        env["MAXMCP_AUTH_TOKEN"] = token
        env["MAXMCP_AUTH_TOKEN_FILE"] = install.expand_path(token_file)

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=4)
    return {"client": client, "updated": True, "path": str(config_path)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rotate MAXMCP auth token and update local client configs.",
    )
    parser.add_argument(
        "--clients",
        default="codex",
        help="Comma-separated client list (codex,cursor,claude,vscode). Default: codex",
    )
    parser.add_argument(
        "--token-file",
        default=install.DEFAULT_AUTH_TOKEN_FILE,
        help=f"Token file path (default: {install.DEFAULT_AUTH_TOKEN_FILE})",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Optional explicit token value (otherwise generated).",
    )
    parser.add_argument(
        "--create-entry",
        action="store_true",
        help="Create MaxMSP MCP entry when missing for JSON clients.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON summary only.",
    )
    args = parser.parse_args()

    selected_clients = [c.strip().lower() for c in args.clients.split(",") if c.strip()]
    allowed = set(install.CONFIG_PATHS.keys())
    unknown = [c for c in selected_clients if c not in allowed]
    if unknown:
        print(json.dumps({"ok": False, "error": f"Unsupported clients: {unknown}"}))
        return 2

    token = (args.token or "").strip() or secrets.token_urlsafe(32)
    token_file_path = Path(install.expand_path(args.token_file))
    _write_token_file(token_file_path, token)

    current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    results = []
    for client in selected_clients:
        cfg_path = Path(install.expand_path(install.CONFIG_PATHS[client]))
        if client == "codex":
            install.install_codex_config(
                cfg_path,
                current_dir,
                auth_token_override=token,
                auth_token_file=str(token_file_path),
            )
            results.append({"client": client, "updated": True, "path": str(cfg_path)})
        else:
            results.append(
                _update_json_client(
                    client=client,
                    config_path=cfg_path,
                    current_dir=current_dir,
                    token=token,
                    token_file=str(token_file_path),
                    create_entry=args.create_entry,
                )
            )

    summary = {
        "ok": True,
        "token_rotated": True,
        "token_file": str(token_file_path),
        "token_preview": f"{token[:6]}...{token[-4:]}",
        "updated_clients": results,
        "next_steps": [
            "Restart MCP client session so new auth env values are loaded.",
            "Run scripts/check_fast.py and scripts/check_live.py to verify connectivity.",
        ],
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
