from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from typing import Any
import uuid

from .json_utils import (
    canonical_json,
    parse_json_object_text,
    parse_json_text,
    pretty_json,
    read_json_file,
    write_json_file,
)
from .process_utils import run_command, run_command_json_object


MAXDIFF_SCRIPT_BY_SUFFIX = {
    ".maxpat": Path("maxdiff") / "maxpat_textconv.py",
    ".amxd": Path("maxdiff") / "amxd_textconv.py",
    ".als": Path("maxdiff") / "als_textconv.py",
}


def maxdiff_script_for_path(path: Path, *, maxdevtools_root: Path) -> Path | None:
    rel = MAXDIFF_SCRIPT_BY_SUFFIX.get(path.suffix.lower())
    if rel is None:
        return None
    candidate = (maxdevtools_root / rel).resolve()
    if candidate.exists():
        return candidate
    return None


def render_text_for_diff(
    path: Path,
    *,
    prefer_maxdiff: bool,
    maxdevtools_root: Path,
) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    if prefer_maxdiff:
        script = maxdiff_script_for_path(path, maxdevtools_root=maxdevtools_root)
        if script is not None:
            try:
                proc = run_command(
                    ["python3", str(script), str(path)],
                    timeout=20,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout, "maxdiff", warnings
                stderr = proc.stderr.strip()
                warnings.append(
                    f"maxdiff exited non-zero for {path.name}: "
                    f"code={proc.returncode} stderr={stderr[:240]}"
                )
            except Exception as exc:
                warnings.append(f"maxdiff failed for {path.name}: {exc}")
        else:
            warnings.append(
                "maxdiff not available for this extension or MAXMCP_MAXDEVTOOLS_ROOT is missing."
            )

    suffix = path.suffix.lower()
    raw = path.read_bytes()
    if suffix in {".amxd", ".als"}:
        digest = hashlib.sha256(raw).hexdigest()[:16]
        return f"<binary:{suffix} size={len(raw)} sha256={digest}>", "internal", warnings

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        digest = hashlib.sha256(raw).hexdigest()[:16]
        return f"<binary size={len(raw)} sha256={digest}>", "internal", warnings

    if suffix in {".maxpat", ".json"}:
        payload, _parse_error = parse_json_text(text)
        if payload is not None:
            return pretty_json(payload, sort_keys=True), "internal", warnings
        return text, "internal", warnings

    return text, "internal", warnings


def write_publish_report(label: str, payload: dict, *, report_dir: Path) -> dict:
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label).strip("_") or "publish_readiness"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{safe_label}_{timestamp}.json"

    signature = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]
    envelope = {
        "schema_version": "maxmcp.publish_readiness.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "signature": signature,
        "report": payload,
    }
    write_json_file(report_path, envelope)
    return {
        "path": str(report_path),
        "signature": signature,
        "size_bytes": report_path.stat().st_size,
    }


def resolve_maxpylang_extended_command(input_path: Path, *, override: str) -> list[str] | None:
    override_value = override.strip()
    if override_value:
        tokens = shlex.split(override_value)
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


def run_maxpylang_check_extended_from_topology(
    topology: Any,
    *,
    timeout_seconds: float,
    report_dir: Path,
    command_override: str,
) -> dict:
    bounded_timeout = max(1.0, min(float(timeout_seconds), 120.0))
    report: dict[str, Any] = {
        "enabled": True,
        "available": True,
        "passed": False,
        "failures": [],
        "warnings": [],
        "metrics": {},
        "command": [],
        "input_path": "",
    }
    if not isinstance(topology, dict):
        report["available"] = False
        report["failures"] = ["topology payload is unavailable for extended validation."]
        report["metrics"] = {"timeout_seconds": bounded_timeout}
        return report

    patcher_payload = topology.get("patcher") if "patcher" in topology else topology
    if not isinstance(patcher_payload, dict):
        report["available"] = False
        report["failures"] = ["topology payload is not a valid patcher object."]
        report["metrics"] = {"timeout_seconds": bounded_timeout}
        return report

    staging_dir = report_dir / "extended_check_inputs"
    staging_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    input_path = staging_dir / f"extended_check_{timestamp}_{uuid.uuid4().hex[:8]}.maxpat"
    write_json_file(input_path, {"patcher": patcher_payload})
    report["input_path"] = str(input_path)

    cmd = resolve_maxpylang_extended_command(input_path, override=command_override)
    if cmd is None:
        report["available"] = False
        report["failures"] = [
            "Unable to locate maxpylang executable. "
            "Set MAXMCP_MAXPYLANG_CHECK_EXTENDED_CMD to configure the command."
        ]
        report["metrics"] = {"timeout_seconds": bounded_timeout}
        return report

    report["command"] = cmd
    started_at = time.perf_counter()
    try:
        proc, parsed, parse_error = run_command_json_object(
            cmd,
            timeout=bounded_timeout,
        )
    except subprocess.TimeoutExpired:
        report["available"] = True
        report["passed"] = False
        report["failures"] = [f"maxpylang_check_extended timed out after {bounded_timeout:.1f}s."]
        report["metrics"] = {
            "timeout_seconds": bounded_timeout,
            "duration_seconds": round(time.perf_counter() - started_at, 3),
            "exit_code": None,
        }
        return report
    except Exception as exc:
        report["available"] = False
        report["passed"] = False
        report["failures"] = [f"maxpylang_check_extended execution failed: {exc}"]
        report["metrics"] = {
            "timeout_seconds": bounded_timeout,
            "duration_seconds": round(time.perf_counter() - started_at, 3),
            "exit_code": None,
        }
        return report

    report["metrics"] = {
        "timeout_seconds": bounded_timeout,
        "duration_seconds": round(time.perf_counter() - started_at, 3),
        "exit_code": int(proc.returncode),
        "stdout_chars": len(proc.stdout or ""),
        "stderr_chars": len(proc.stderr or ""),
    }
    if parsed is None:
        report["passed"] = False
        report["failures"] = [
            (
                "maxpylang_check_extended did not emit valid JSON output"
                + (f": {parse_error}" if parse_error else ".")
            )
        ]
        stderr_excerpt = (proc.stderr or "").strip()
        if stderr_excerpt:
            report["warnings"].append(stderr_excerpt[:240])
        return report

    warnings = parsed.get("warnings")
    errors = parsed.get("errors")
    report["warnings"] = list(warnings) if isinstance(warnings, list) else []
    reported_failures = list(errors) if isinstance(errors, list) else []
    if not reported_failures and proc.returncode != 0:
        stderr_excerpt = (proc.stderr or "").strip()
        if stderr_excerpt:
            reported_failures.append(stderr_excerpt[:240])
        else:
            reported_failures.append("maxpylang_check_extended exited non-zero.")

    changes = parsed.get("changes") if isinstance(parsed.get("changes"), dict) else {}
    report["metrics"].update(
        {
            "unknowns": int(changes.get("unknowns", 0) or 0),
            "js_unlinked": int(changes.get("js_unlinked", 0) or 0),
            "abstractions": int(changes.get("abstractions", 0) or 0),
        }
    )
    report["raw"] = {
        "ok": bool(parsed.get("ok", False)),
        "schema": parsed.get("schema"),
        "message": parsed.get("message"),
    }
    report["passed"] = bool(parsed.get("ok", False)) and proc.returncode == 0
    report["failures"] = reported_failures
    return report


def evaluate_chaos_gate(
    chaos_report_path: str,
    *,
    require_chaos_gate: bool,
) -> tuple[dict, list[dict], list[dict]]:
    payload: dict[str, Any] = {
        "required": bool(require_chaos_gate),
        "executed": False,
        "passed": None,
        "failures": [],
        "warnings": [],
        "report_path": "",
    }
    gate_failures: list[dict] = []
    gate_warnings: list[dict] = []

    report_path_raw = str(chaos_report_path or "").strip()
    if not report_path_raw:
        if require_chaos_gate:
            payload["passed"] = False
            payload["failures"] = ["Chaos gate is required but no report path was provided."]
            gate_failures.append(
                {
                    "gate": "chaos_gate",
                    "message": "Chaos gate is required but no report path was provided.",
                    "details": {},
                }
            )
        return payload, gate_failures, gate_warnings

    report_path = Path(report_path_raw).expanduser()
    if not report_path.is_absolute():
        report_path = (Path.cwd() / report_path).resolve()
    else:
        report_path = report_path.resolve()
    payload["report_path"] = str(report_path)

    if not report_path.exists() or not report_path.is_file():
        payload["passed"] = False
        payload["failures"] = [f"Chaos report path does not exist: {report_path}"]
        gate_failures.append(
            {
                "gate": "chaos_gate",
                "message": "Chaos report path does not exist.",
                "details": {"path": str(report_path)},
            }
        )
        return payload, gate_failures, gate_warnings

    try:
        parsed = read_json_file(report_path)
    except Exception as exc:
        payload["passed"] = False
        payload["failures"] = [f"Failed to parse chaos report JSON: {exc}"]
        gate_failures.append(
            {
                "gate": "chaos_gate",
                "message": "Failed to parse chaos report JSON.",
                "details": {"path": str(report_path), "error": str(exc)},
            }
        )
        return payload, gate_failures, gate_warnings
    if not isinstance(parsed, dict):
        payload["passed"] = False
        payload["failures"] = ["Chaos report JSON is not an object."]
        gate_failures.append(
            {
                "gate": "chaos_gate",
                "message": "Chaos report JSON is not an object.",
                "details": {"path": str(report_path)},
            }
        )
        return payload, gate_failures, gate_warnings

    payload["executed"] = True
    payload["summary"] = parsed.get("summary") if isinstance(parsed.get("summary"), dict) else {}
    aggregate_slo = parsed.get("aggregate_slo") if isinstance(parsed.get("aggregate_slo"), dict) else {}
    payload["aggregate_slo"] = aggregate_slo
    gate_passed = bool(parsed.get("ok", False))
    if aggregate_slo:
        gate_passed = gate_passed and bool(aggregate_slo.get("passed", False))
    payload["passed"] = gate_passed

    scenario_results = parsed.get("scenario_results")
    if isinstance(scenario_results, list):
        payload["scenario_results"] = scenario_results
        failed_scenarios = [
            item for item in scenario_results if isinstance(item, dict) and not item.get("ok", False)
        ]
        if failed_scenarios:
            gate_warnings.append(
                {
                    "gate": "chaos_gate",
                    "message": "One or more chaos scenarios reported failures.",
                    "details": {"count": len(failed_scenarios)},
                }
            )

    if not gate_passed:
        payload["failures"] = [
            "Chaos gate did not pass aggregate SLO checks."
            if aggregate_slo
            else "Chaos gate reported failure."
        ]
        gate_failures.append(
            {
                "gate": "chaos_gate",
                "message": payload["failures"][0],
                "details": {"path": str(report_path), "aggregate_slo": aggregate_slo},
            }
        )
    return payload, gate_failures, gate_warnings
