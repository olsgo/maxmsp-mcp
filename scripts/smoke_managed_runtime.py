#!/usr/bin/env python3
"""One-command smoke test for managed MaxMSP MCP runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import server  # noqa: E402


def _print_step(name: str, payload: dict) -> None:
    print(f"[{name}]")
    print(json.dumps(payload, indent=2, sort_keys=True))


async def _run(timeout_ready: float) -> int:
    conn = server.MaxMSPConnection(
        server.SOCKETIO_SERVER_URL,
        server.SOCKETIO_SERVER_PORT,
        server.NAMESPACE,
    )
    runtime = server.MaxRuntimeManager(conn)
    conn.runtime_manager = runtime

    created_varname = None
    try:
        status = await runtime.ensure_runtime_ready()
        _print_step(
            "ensure_runtime_ready",
            {
                "ready": status.get("ready"),
                "bridge_connected": status.get("bridge_connected"),
                "bridge_healthy": status.get("bridge_healthy"),
                "max_app_exists": status.get("max_app_exists"),
                "host_patch_exists": status.get("host_patch_exists"),
                "node_modules_ready": status.get("node_modules_ready"),
                "host_patch_path": status.get("host_patch_path"),
                "error": status.get("error"),
            },
        )
        if not status.get("ready"):
            return 2

        started_at = time.monotonic()
        while time.monotonic() - started_at < timeout_ready:
            health = await runtime.collect_status(check_bridge=True)
            if health.get("bridge_connected") and health.get("bridge_healthy"):
                _print_step(
                    "bridge_status",
                    {
                        "bridge_connected": health.get("bridge_connected"),
                        "bridge_healthy": health.get("bridge_healthy"),
                        "bridge_ping": health.get("bridge_ping"),
                        "active_target": health.get("active_target"),
                    },
                )
                break
            await asyncio.sleep(0.5)
        else:
            _print_step("bridge_status", {"error": "bridge health check timed out"})
            return 3

        avoid_rect = await conn.send_request({"action": "get_avoid_rect_position"}, timeout=5.0)
        if not isinstance(avoid_rect, list) or len(avoid_rect) < 4:
            _print_step(
                "get_avoid_rect_position",
                {"error": "unexpected avoid rect format", "value": avoid_rect},
            )
            return 4

        # Empty patchers may return [None, None, None, None]; fall back to a safe origin.
        right = avoid_rect[2]
        top = avoid_rect[1]
        if not isinstance(right, (int, float)):
            right = 80
        if not isinstance(top, (int, float)):
            top = 80

        x = int(right) + 40
        y = int(top) + 40
        created_varname = f"smoke_runtime_{int(time.time())}"

        add_result = await conn.send_request(
            {
                "action": "add_object",
                "position": [x, y],
                "obj_type": "button",
                "args": [],
                "varname": created_varname,
            },
            timeout=8.0,
        )
        _print_step(
            "add_object",
            {
                "varname": created_varname,
                "position": [x, y],
                "result": add_result,
            },
        )

        remove_result = await conn.send_request(
            {"action": "remove_object", "varname": created_varname},
            timeout=5.0,
        )
        _print_step(
            "remove_object",
            {
                "varname": created_varname,
                "result": remove_result,
            },
        )
        created_varname = None

        print("[result]")
        print("PASS")
        return 0
    except Exception as exc:  # pragma: no cover - smoke script error path
        _print_step("error", {"type": type(exc).__name__, "message": str(exc)})
        return 1
    finally:
        if created_varname:
            try:
                await conn.send_request(
                    {"action": "remove_object", "varname": created_varname},
                    timeout=5.0,
                )
            except Exception:
                pass
        await conn.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test managed Max runtime and bridge.")
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=20.0,
        help="Seconds to wait for bridge health after runtime start (default: 20.0).",
    )
    args = parser.parse_args()
    return asyncio.run(_run(timeout_ready=args.ready_timeout))


if __name__ == "__main__":
    raise SystemExit(main())
