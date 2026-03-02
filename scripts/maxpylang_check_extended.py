#!/usr/bin/env python3
"""Run extended MaxPyLang validation and emit gate-friendly JSON."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_json_from_output(raw: str) -> tuple[dict | None, str | None]:
    text = (raw or "").strip()
    if not text:
        return None, "empty output"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, str(exc)
    if not isinstance(parsed, dict):
        return None, "JSON payload is not an object"
    return parsed, None


def _resolve_command(input_path: Path, command_override: str = "") -> list[str] | None:
    override = (command_override or "").strip() or os.environ.get(
        "MAXMCP_MAXPYLANG_CHECK_EXTENDED_CMD",
        "",
    ).strip()
    if override:
        tokens = shlex.split(override)
        if not tokens:
            return None
        rendered: list[str] = []
        placeholder_used = False
        for token in tokens:
            if "{input_path}" in token:
                rendered.append(token.replace("{input_path}", str(input_path)))
                placeholder_used = True
            else:
                rendered.append(token)
        if not placeholder_used:
            rendered.extend(["--in", str(input_path)])
        return rendered

    maxpylang_bin = shutil.which("maxpylang")
    if not maxpylang_bin:
        return None
    return [
        maxpylang_bin,
        "--json",
        "--strict",
        "check",
        "--unknown",
        "--js",
        "--abstractions",
        "--in",
        str(input_path),
    ]


def _build_summary(
    *,
    ok: bool,
    command: list[str],
    input_path: Path,
    duration_seconds: float,
    failures: list[str],
    warnings: list[str],
    parsed_payload: dict | None,
    parse_error: str | None,
    exit_code: int | None,
) -> dict:
    changes = parsed_payload.get("changes") if isinstance(parsed_payload, dict) else {}
    if not isinstance(changes, dict):
        changes = {}
    summary = {
        "passed": bool(ok),
        "failures": [str(item) for item in failures],
        "warnings": [str(item) for item in warnings],
        "metrics": {
            "unknowns": int(changes.get("unknowns", 0) or 0),
            "js_unlinked": int(changes.get("js_unlinked", 0) or 0),
            "abstractions": int(changes.get("abstractions", 0) or 0),
        },
    }
    return {
        "ok": bool(ok),
        "command": command,
        "input_path": str(input_path),
        "duration_seconds": round(float(duration_seconds), 3),
        "summary": summary,
        "result": {
            "exit_code": exit_code,
            "parse_error": parse_error,
            "schema": parsed_payload.get("schema") if isinstance(parsed_payload, dict) else None,
            "message": parsed_payload.get("message") if isinstance(parsed_payload, dict) else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run maxpylang extended check for release gates.")
    parser.add_argument(
        "--input",
        type=Path,
        default=REPO_ROOT / "tests" / "fixtures" / "import_text_boxes.maxpat",
        help="Patch file path to validate (default: tests/fixtures/import_text_boxes.maxpat).",
    )
    parser.add_argument(
        "--command",
        default="",
        help=(
            "Optional command override. Use {input_path} placeholder if needed; "
            "otherwise '--in <path>' is appended automatically."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="Command timeout in seconds (default: 180).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summary.")
    parser.add_argument("--dry-run", action="store_true", help="Emit deterministic dry-run payload.")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    if not input_path.is_absolute():
        input_path = (REPO_ROOT / input_path).resolve()
    else:
        input_path = input_path.resolve()

    if args.dry_run:
        payload = _build_summary(
            ok=True,
            command=["maxpylang", "--json", "--strict", "check", "--in", str(input_path)],
            input_path=input_path,
            duration_seconds=0.0,
            failures=[],
            warnings=[],
            parsed_payload={
                "schema": "maxpylang.cli.check.success.v1",
                "message": "dry-run success",
                "changes": {"unknowns": 0, "js_unlinked": 0, "abstractions": 0},
            },
            parse_error=None,
            exit_code=0,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not input_path.exists() or not input_path.is_file():
        payload = _build_summary(
            ok=False,
            command=[],
            input_path=input_path,
            duration_seconds=0.0,
            failures=[f"Input patch path does not exist: {input_path}"],
            warnings=[],
            parsed_payload=None,
            parse_error=None,
            exit_code=None,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    cmd = _resolve_command(input_path, command_override=str(args.command or ""))
    if cmd is None:
        payload = _build_summary(
            ok=False,
            command=[],
            input_path=input_path,
            duration_seconds=0.0,
            failures=[
                "Unable to resolve maxpylang executable. Set MAXMCP_MAXPYLANG_CHECK_EXTENDED_CMD or --command."
            ],
            warnings=[],
            parsed_payload=None,
            parse_error=None,
            exit_code=None,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=max(1, int(args.timeout_seconds)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        payload = _build_summary(
            ok=False,
            command=cmd,
            input_path=input_path,
            duration_seconds=time.perf_counter() - started,
            failures=[f"maxpylang check timed out after {int(args.timeout_seconds)}s"],
            warnings=[],
            parsed_payload=None,
            parse_error=None,
            exit_code=None,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1
    except Exception as exc:
        payload = _build_summary(
            ok=False,
            command=cmd,
            input_path=input_path,
            duration_seconds=time.perf_counter() - started,
            failures=[f"maxpylang check failed to execute: {exc}"],
            warnings=[],
            parsed_payload=None,
            parse_error=None,
            exit_code=None,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    parsed, parse_error = _parse_json_from_output(proc.stdout or "")
    warnings: list[str] = []
    failures: list[str] = []
    if parsed is None:
        failures.append(
            "maxpylang check did not emit parseable JSON output"
            + (f": {parse_error}" if parse_error else ".")
        )
        stderr_excerpt = (proc.stderr or "").strip()
        if stderr_excerpt:
            warnings.append(stderr_excerpt[:240])
    else:
        maybe_warnings = parsed.get("warnings")
        maybe_errors = parsed.get("errors")
        if isinstance(maybe_warnings, list):
            warnings = [str(item) for item in maybe_warnings]
        if isinstance(maybe_errors, list):
            failures.extend(str(item) for item in maybe_errors)
        if proc.returncode != 0 and not failures:
            stderr_excerpt = (proc.stderr or "").strip()
            failures.append(stderr_excerpt[:240] if stderr_excerpt else "maxpylang check returned non-zero exit code.")

    ok = proc.returncode == 0 and isinstance(parsed, dict) and bool(parsed.get("ok", False))
    payload = _build_summary(
        ok=ok,
        command=cmd,
        input_path=input_path,
        duration_seconds=time.perf_counter() - started,
        failures=failures,
        warnings=warnings,
        parsed_payload=parsed,
        parse_error=parse_error,
        exit_code=int(proc.returncode),
    )

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
        if proc.stdout.strip():
            print(proc.stdout.rstrip())
        if proc.stderr.strip():
            print(proc.stderr.rstrip(), file=sys.stderr)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
