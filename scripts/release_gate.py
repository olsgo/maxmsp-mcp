#!/usr/bin/env python3
"""Run tiered validation profiles for maxmsp-mcp."""

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
    parsed = None
    parse_error = None
    stdout = proc.stdout or ""
    if stdout.strip():
        try:
            maybe = json.loads(stdout)
            if isinstance(maybe, dict):
                parsed = maybe
        except json.JSONDecodeError as exc:
            parse_error = str(exc)
    return {
        "name": name,
        "command": cmd,
        "exit_code": proc.returncode,
        "duration_seconds": round(ended - started, 3),
        "stdout": stdout,
        "stderr": proc.stderr or "",
        "ok": proc.returncode == 0,
        "parsed_json": parsed,
        "parse_error": parse_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run maxmsp-mcp validation gate profiles.")
    parser.add_argument(
        "--profile",
        choices=["fast", "full", "release"],
        default="full",
        help="Validation profile: fast, full, or release.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summary only.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-stage output.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=2400,
        help="Per-stage timeout in seconds (default: 2400).",
    )
    parser.add_argument(
        "--soak-seconds",
        type=int,
        default=1800,
        help="Soak duration passed to live checks for release profile (default: 1800).",
    )
    parser.add_argument(
        "--chaos-timeout-seconds",
        type=int,
        default=900,
        help="Chaos stage timeout passed to live checks (default: 900).",
    )
    args = parser.parse_args()

    stages: list[tuple[str, list[str], int]] = [
        (
            "check_fast",
            [str(PYTHON), "scripts/check_fast.py", "--json", "--stop-on-fail"],
            max(300, int(args.timeout_seconds)),
        )
    ]
    if args.profile == "fast":
        live_cmd = [
            str(PYTHON),
            "scripts/check_live.py",
            "--json",
            "--profile",
            "quick",
            "--run-chaos",
            "--chaos-preset",
            "pr",
            "--chaos-timeout-seconds",
            str(int(args.chaos_timeout_seconds)),
        ]
        stages.append(("check_live", live_cmd, max(1200, int(args.timeout_seconds))))
    elif args.profile in {"full", "release"}:
        live_cmd = [str(PYTHON), "scripts/check_live.py", "--json"]
        if args.profile == "full":
            live_cmd.extend(
                [
                    "--profile",
                    "full",
                    "--skip-live-soak",
                    "--run-chaos",
                    "--chaos-preset",
                    "pr",
                    "--chaos-timeout-seconds",
                    str(int(args.chaos_timeout_seconds)),
                ]
            )
        else:
            live_cmd.extend(
                [
                    "--profile",
                    "release",
                    "--soak-seconds",
                    str(int(args.soak_seconds)),
                    "--run-chaos",
                    "--chaos-preset",
                    "full",
                    "--chaos-timeout-seconds",
                    str(int(args.chaos_timeout_seconds)),
                ]
            )
        stages.append(("check_live", live_cmd, max(1200, int(args.timeout_seconds))))

    results: list[dict] = []
    for name, cmd, timeout in stages:
        stage = _run_stage(name, cmd, timeout)
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
        "ok": all(stage["ok"] for stage in results),
        "profile": args.profile,
        "stages": [
            {
                "name": stage["name"],
                "ok": stage["ok"],
                "exit_code": stage["exit_code"],
                "duration_seconds": stage["duration_seconds"],
                "parse_error": stage["parse_error"],
                "parsed_json": stage["parsed_json"],
            }
            for stage in results
        ],
        "total_duration_seconds": round(sum(stage["duration_seconds"] for stage in results), 3),
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
