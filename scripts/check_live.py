#!/usr/bin/env python3
"""Run gated live bridge checks against a real Max runtime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _run_stage(
    name: str,
    cmd: list[str],
    timeout: int,
    env: dict[str, str],
) -> dict:
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    ended = time.time()
    return {
        "name": name,
        "command": cmd,
        "exit_code": proc.returncode,
        "duration_seconds": round(ended - started, 3),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "ok": proc.returncode == 0,
    }


def _has_blocked_warning(stage: dict) -> bool:
    combined = f"{stage.get('stdout', '')}\n{stage.get('stderr', '')}"
    blocked = [
        "DeprecationWarning",
        "parameter 'timeout' of type 'float' is deprecated",
    ]
    return all(token in combined for token in blocked)


def _preflight() -> tuple[bool, dict]:
    try:
        sys.path.insert(0, str(REPO_ROOT))
        import server  # noqa: WPS433

        return True, {
            "max_app_path": str(server.MAX_APP_PATH),
            "max_app_exists": server.MAX_APP_PATH.exists(),
            "host_patch_path": str(server.HOST_PATCH_PATH),
            "host_patch_exists": server.HOST_PATCH_PATH.exists(),
        }
    except Exception as e:  # pragma: no cover - defensive
        return False, {"error": str(e)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live bridge checks (opt-in).")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary only.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-stage output.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="Per-stage timeout in seconds (default: 900).",
    )
    parser.add_argument(
        "--allow-deprecation-warning",
        action="store_true",
        help="Allow blocked deprecation warning signatures in live test output.",
    )
    args = parser.parse_args()

    preflight_ok, preflight_data = _preflight()
    if not preflight_ok:
        print(json.dumps({"ok": False, "preflight": preflight_data}, indent=2, sort_keys=True))
        return 1

    env = os.environ.copy()
    env["MAXMCP_RUN_LIVE_E2E"] = "1"

    stages = [
        (
            "smoke_managed_runtime",
            [str(PYTHON), "scripts/smoke_managed_runtime.py", "--ready-timeout", "30"],
        ),
        (
            "live_bridge_e2e",
            [str(PYTHON), "-m", "unittest", "-v", "tests/test_live_bridge_e2e.py"],
        ),
        (
            "queue_soak_synthetic",
            [str(PYTHON), "-m", "unittest", "-v", "tests/test_soak_queue.py"],
        ),
    ]

    results: list[dict] = []
    for name, cmd in stages:
        stage = _run_stage(name, cmd, timeout=args.timeout_seconds, env=env)
        if (
            name == "live_bridge_e2e"
            and not args.allow_deprecation_warning
            and _has_blocked_warning(stage)
        ):
            stage["ok"] = False
            stage["exit_code"] = stage["exit_code"] or 1
            stage["stderr"] = (
                stage.get("stderr", "")
                + "\nBlocked deprecation warning detected in live bridge output."
            ).strip()
        results.append(stage)
        if not args.quiet and not args.json:
            print(f"[{name}] {'PASS' if stage['ok'] else 'FAIL'} ({stage['duration_seconds']}s)")
            if stage["stdout"].strip():
                print(stage["stdout"].rstrip())
            if stage["stderr"].strip():
                print(stage["stderr"].rstrip(), file=sys.stderr)
        if not stage["ok"]:
            break

    summary = {
        "ok": all(r["ok"] for r in results),
        "preflight": preflight_data,
        "stages": [
            {
                "name": r["name"],
                "ok": r["ok"],
                "exit_code": r["exit_code"],
                "duration_seconds": r["duration_seconds"],
            }
            for r in results
        ],
        "total_duration_seconds": round(sum(r["duration_seconds"] for r in results), 3),
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    elif not args.quiet:
        print("\n[summary]")
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(json.dumps(summary, sort_keys=True))

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
