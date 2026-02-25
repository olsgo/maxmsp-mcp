#!/usr/bin/env python3
"""Run fast local validation checks for maxmsp-mcp."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _run_stage(name: str, cmd: list[str], timeout: int) -> dict:
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fast local checks.")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary only.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-stage output.")
    parser.add_argument(
        "--stop-on-fail",
        action="store_true",
        help="Stop execution at first failing stage.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="Per-stage timeout in seconds (default: 300).",
    )
    args = parser.parse_args()

    py_files = sorted(str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "scripts").glob("*.py"))
    test_files = sorted(str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "tests").glob("test_*.py"))

    stages = [
        (
            "python_compile",
            [str(PYTHON), "-m", "py_compile", "server.py", "install.py", *py_files, *test_files],
        ),
        (
            "node_check",
            [
                "node",
                "--check",
                "MaxMSP_Agent/max_mcp.js",
            ],
        ),
        (
            "node_check_v8",
            [
                "node",
                "--check",
                "MaxMSP_Agent/max_mcp_v8_add_on.js",
            ],
        ),
        (
            "node_check_bridge",
            [
                "node",
                "--check",
                "MaxMSP_Agent/max_mcp_node.js",
            ],
        ),
        (
            "unit_tests",
            [
                str(PYTHON),
                "-m",
                "unittest",
                "-v",
                "tests/test_install.py",
                "tests/test_protocol.py",
                "tests/test_rotate_auth_token.py",
                "tests/test_soak_queue.py",
            ],
        ),
    ]

    results: list[dict] = []
    for name, cmd in stages:
        stage = _run_stage(name, cmd, timeout=args.timeout_seconds)
        results.append(stage)
        if not args.quiet and not args.json:
            print(f"[{name}] {'PASS' if stage['ok'] else 'FAIL'} ({stage['duration_seconds']}s)")
            if stage["stdout"].strip():
                print(stage["stdout"].rstrip())
            if stage["stderr"].strip():
                print(stage["stderr"].rstrip(), file=sys.stderr)
        if args.stop_on_fail and not stage["ok"]:
            break

    summary = {
        "ok": all(r["ok"] for r in results),
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
