import os
import json
import argparse
import re
import secrets
from pathlib import Path

CONFIG_PATHS = {
    "claude": (
        "~/Library/Application Support/Claude/claude_desktop_config.json"
        if os.name == "posix"  # macOS or Linux
        else r"%APPDATA%\Claude\claude_desktop_config.json"  # Windows
    ),
    "cursor": "~/.cursor/mcp.json",
    "vscode": ".vscode/mcp.json",
    "codex": "~/.codex/config.toml",
}
DEFAULT_AUTH_TOKEN_FILE = "~/.maxmsp-mcp/auth_token"


def expand_path(path):
    # Expand ~
    path = os.path.expanduser(path)
    # Expand environment variables like %APPDATA%
    path = os.path.expandvars(path)
    # Normalize and convert to absolute path
    return os.path.abspath(path)


def load_json(file_path: Path):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    # Not exist or is empty
    if not file_path.exists() or file_path.stat().st_size == 0:
        # Create the file with an empty JSON object
        with open(file_path, "w") as f:
            json.dump({"mcpServers": {}}, f)
    # Load the JSON data
    with open(file_path, "r") as f:
        return json.load(f)


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def remove_toml_tables(toml_text: str, table_prefix: str) -> str:
    lines = toml_text.splitlines(keepends=True)
    kept = []
    skip_current_table = False
    header_pattern = re.compile(r"^\s*\[([^\]]+)\]\s*$")

    for line in lines:
        header_match = header_pattern.match(line)
        if header_match:
            table_name = header_match.group(1).strip()
            if table_name == table_prefix or table_name.startswith(f"{table_prefix}."):
                skip_current_table = True
            else:
                skip_current_table = False

        if not skip_current_table:
            kept.append(line)

    return "".join(kept).rstrip()


def _normalize_auth_token(token: str | None) -> str:
    if not isinstance(token, str):
        return ""
    return token.strip()


def resolve_auth_token(existing_token: str | None = None) -> str:
    token = _normalize_auth_token(existing_token)
    if token:
        return token
    return secrets.token_urlsafe(32)


def extract_codex_auth_token(toml_text: str) -> str:
    match = re.search(r'^\s*MAXMCP_AUTH_TOKEN\s*=\s*"([^"]+)"\s*$', toml_text, re.MULTILINE)
    if not match:
        return ""
    return _normalize_auth_token(match.group(1))


def build_common_env(
    current_dir: str,
    auth_token: str,
    auth_token_file: str = DEFAULT_AUTH_TOKEN_FILE,
) -> dict:
    return {
        "SOCKETIO_SERVER_URL": "http://127.0.0.1",
        "SOCKETIO_SERVER_PORT": "5002",
        "NAMESPACE": "/mcp",
        "MAXMCP_MANAGED_MODE": "1",
        "MAXMCP_HOST_PATCH": os.path.join(current_dir, "MaxMSP_Agent", "mcp_host.maxpat"),
        "MAXMCP_NPM_AUTO_INSTALL": "1",
        "MAXMCP_STRICT_V2_ENFORCEMENT": "1",
        "MAXMCP_STRICT_CAPABILITY_GATING": "1",
        "MAXMCP_MUTATION_MAX_INFLIGHT": "4",
        "MAXMCP_MUTATION_MAX_QUEUE": "64",
        "MAXMCP_MUTATION_QUEUE_WAIT_TIMEOUT_SECONDS": "15",
        "MAXMCP_PREFLIGHT_MODE": "auto",
        "MAXMCP_PREFLIGHT_CACHE_SECONDS": "30",
        "MAXMCP_WORKSPACE_CAPTURE_TIMEOUT_SECONDS": "8",
        "MAXMCP_WORKSPACE_CAPTURE_RETRIES": "2",
        "MAXMCP_WORKSPACE_CAPTURE_BACKOFF_SECONDS": "0.5",
        "MAXMCP_REQUIRE_HANDSHAKE_AUTH": "1",
        "MAXMCP_ALLOW_REMOTE": "0",
        "MAXMCP_ENFORCE_PATCH_ROOTS": "0",
        "MAXMCP_ALLOWED_PATCH_ROOTS": "",
        "MAXMCP_HYGIENE_AUTO_CLEANUP": "1",
        "MAXMCP_HYGIENE_SCOPE": "all_max_instances",
        "MAXMCP_HYGIENE_MODE": "aggressive",
        "MAXMCP_HYGIENE_STALE_SECONDS": "1800",
        "MAXMCP_HYGIENE_STARTUP_SWEEP": "1",
        "MAXMCP_HYGIENE_MAX_KILLS_PER_SWEEP": "50",
        "MAXMCP_AUTH_TOKEN": auth_token,
        "MAXMCP_AUTH_TOKEN_FILE": expand_path(auth_token_file),
    }


def install_codex_config(
    config_path: Path,
    current_dir: str,
    auth_token_override: str | None = None,
    auth_token_file: str = DEFAULT_AUTH_TOKEN_FILE,
):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_toml = ""
    if config_path.exists():
        with open(config_path, "r") as f:
            existing_toml = f.read()

    auth_token = resolve_auth_token(auth_token_override or extract_codex_auth_token(existing_toml))
    cleaned_toml = remove_toml_tables(existing_toml, "mcp_servers.maxmsp")
    server_dir = toml_escape(current_dir)
    env = build_common_env(current_dir, auth_token, auth_token_file=auth_token_file)
    env_lines = "".join(
        f'{key} = "{toml_escape(value)}"\n'
        for key, value in env.items()
    )
    codex_block = (
        "[mcp_servers.maxmsp]\n"
        'command = "uv"\n'
        "args = [\n"
        f'  "--directory",\n  "{server_dir}",\n  "run",\n  "server.py",\n'
        "]\n\n"
        "[mcp_servers.maxmsp.env]\n"
        f"{env_lines}"
    )

    if cleaned_toml:
        final_toml = f"{cleaned_toml}\n\n{codex_block}"
    else:
        final_toml = codex_block

    with open(config_path, "w") as f:
        f.write(final_toml)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--client",
        type=str,
        required=True,
        choices=list(CONFIG_PATHS.keys()),
        help=f"Supported clients: {', '.join(CONFIG_PATHS.keys())}",
    )
    args = parser.parse_args()
    config_path = Path(expand_path(CONFIG_PATHS[args.client]))

    current_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(os.path.join(current_dir, ".venv")):
        raise FileNotFoundError("Use uv to create a virtual environment first. ")

    if args.client == "codex":
        install_codex_config(config_path, current_dir)
        return

    config_data = load_json(config_path)
    mcp_name = "mcpServers" if args.client != "vscode" else "servers"
    if mcp_name not in config_data or not isinstance(config_data[mcp_name], dict):
        config_data[mcp_name] = {}

    existing_env = {}
    if isinstance(config_data[mcp_name].get("MaxMSPMCP"), dict):
        maybe_env = config_data[mcp_name]["MaxMSPMCP"].get("env")
        if isinstance(maybe_env, dict):
            existing_env = maybe_env
    auth_token = resolve_auth_token(existing_env.get("MAXMCP_AUTH_TOKEN"))
    common_env = build_common_env(current_dir, auth_token)

    config_data[mcp_name]["MaxMSPMCP"] = {
        "command": "mcp",
        "args": ["run", os.path.join(current_dir, "server.py")],
        "env": {
            "PATH": os.path.join(current_dir, ".venv/bin"),
            "VIRTUAL_ENV": os.path.join(current_dir, ".venv"),
            **common_env,
        },
    }

    with open(config_path, "w") as f:
        json.dump(config_data, f, indent=4)


if __name__ == "__main__":
    main()
