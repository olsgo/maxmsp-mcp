from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import anyio
from mcp.client.sse import sse_client
from mcp.server.stdio import stdio_server


SHARED_DAEMON_MODE = "shared_daemon"
SINGLE_CLIENT_MODE = "single"
SERVER_ROLE_ENV = "MAXMCP_SERVER_ROLE"
SERVER_ROLE_CLIENT = "client"
SERVER_ROLE_DAEMON = "daemon"


def normalize_multi_client_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {SHARED_DAEMON_MODE, SINGLE_CLIENT_MODE}:
        return normalized
    return SHARED_DAEMON_MODE


def normalize_server_role(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == SERVER_ROLE_DAEMON:
        return SERVER_ROLE_DAEMON
    return SERVER_ROLE_CLIENT


def build_sse_url(host: str, port: int, sse_path: str = "/sse") -> str:
    normalized_path = sse_path if str(sse_path or "").startswith("/") else f"/{sse_path}"
    return f"http://{host}:{int(port)}{normalized_path}"


def choose_daemon_port(host: str, preferred_port: int) -> int:
    candidate = int(preferred_port) if int(preferred_port) > 0 else 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, candidate))
        return int(sock.getsockname()[1])


def parse_shared_daemon_payload(
    payload: dict[str, Any],
    *,
    pid_alive: Callable[[int], bool],
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return None
    role = normalize_server_role(payload.get("server_role"))
    share_url = payload.get("share_url")
    if role != SERVER_ROLE_DAEMON or not isinstance(share_url, str) or not share_url:
        return None
    return {
        "pid": pid,
        "hostname": payload.get("hostname"),
        "share_url": share_url,
        "share_host": payload.get("share_host"),
        "share_port": payload.get("share_port"),
        "transport": payload.get("transport"),
        "acquired_at_epoch": payload.get("acquired_at_epoch"),
    }


async def probe_shared_daemon(url: str, timeout_seconds: float) -> bool:
    try:
        async with asyncio.timeout(max(0.1, float(timeout_seconds))):
            async with sse_client(url, timeout=1, sse_read_timeout=1):
                return True
    except Exception:
        return False


async def wait_for_shared_daemon(
    resolve_payload: Callable[[], dict[str, Any] | None],
    *,
    timeout_seconds: float,
    probe_timeout_seconds: float = 1.0,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while time.monotonic() < deadline:
        payload = resolve_payload()
        if payload:
            share_url = payload.get("share_url")
            if isinstance(share_url, str) and share_url:
                if await probe_shared_daemon(share_url, probe_timeout_seconds):
                    return payload
        await asyncio.sleep(0.1)
    return None


async def _forward_jsonrpc_messages(read_stream: Any, write_stream: Any) -> None:
    try:
        async for message in read_stream:
            if isinstance(message, Exception):
                raise message
            await write_stream.send(message)
    finally:
        with contextlib.suppress(Exception):
            await write_stream.aclose()


async def run_stdio_proxy_to_sse(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    async with stdio_server() as (stdio_read, stdio_write):
        async with sse_client(url, headers=headers) as (remote_read, remote_write):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_forward_jsonrpc_messages, stdio_read, remote_write)
                tg.start_soon(_forward_jsonrpc_messages, remote_read, stdio_write)


def launch_shared_daemon_process(
    *,
    server_script: Path,
    host: str,
    port: int,
    log_path: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[Any]:
    env = os.environ.copy()
    env.update(extra_env or {})
    env[SERVER_ROLE_ENV] = SERVER_ROLE_DAEMON
    env["FASTMCP_HOST"] = host
    env["FASTMCP_PORT"] = str(int(port))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [sys.executable, str(server_script)],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(server_script.parent),
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    return process
