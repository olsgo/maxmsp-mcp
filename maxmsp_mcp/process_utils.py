from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from .json_utils import parse_json_object_text


def run_command(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=str(cwd) if cwd is not None else None,
        input=input_text,
    )


def run_command_json_object(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict | None, str | None]:
    proc = run_command(
        args,
        timeout=timeout,
        cwd=cwd,
        input_text=input_text,
    )
    payload, parse_error = parse_json_object_text(proc.stdout or "")
    return proc, payload, parse_error
