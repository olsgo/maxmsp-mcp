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


def _safe_now_label() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _write_artifacts(
    *,
    preflight: dict,
    results: list[dict],
    summary: dict,
    artifacts_dir: Path,
) -> Path:
    run_dir = artifacts_dir / f"live_check_{_safe_now_label()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    for idx, stage in enumerate(results, start=1):
        name = stage.get("name", f"stage_{idx}")
        safe_name = "".join(
            ch if ch.isalnum() or ch in {"_", "-"} else "_"
            for ch in str(name)
        )
        (run_dir / f"{idx:02d}_{safe_name}.stdout.log").write_text(
            stage.get("stdout", "") or "",
            encoding="utf-8",
        )
        (run_dir / f"{idx:02d}_{safe_name}.stderr.log").write_text(
            stage.get("stderr", "") or "",
            encoding="utf-8",
        )
        (run_dir / f"{idx:02d}_{safe_name}.json").write_text(
            json.dumps(stage, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    full_payload = {
        "generated_at_epoch": time.time(),
        "preflight": preflight,
        "summary": summary,
        "stages_full": results,
    }
    (run_dir / "summary_full.json").write_text(
        json.dumps(full_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return run_dir


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


def _parse_json_from_output(raw: str) -> tuple[dict | None, str | None]:
    text = (raw or "").strip()
    if not text:
        return None, "empty output"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid JSON output"
    if not isinstance(parsed, dict):
        return None, "JSON payload is not an object"
    return parsed, None


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
    parser.add_argument(
        "--profile",
        choices=["quick", "full", "release"],
        default="full",
        help="Validation profile: quick (no live soak), full, or release (strict SLO gating).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summary only.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-stage output.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1200,
        help="Per-stage timeout floor in seconds (default: 1200).",
    )
    parser.add_argument(
        "--skip-extended-check",
        action="store_true",
        help="Skip maxpylang extended-check stage.",
    )
    parser.add_argument(
        "--extended-check-timeout-seconds",
        type=int,
        default=180,
        help="Timeout for maxpylang extended-check stage (default: 180).",
    )
    parser.add_argument(
        "--run-chaos",
        action="store_true",
        help="Run chaos fault-injection stage.",
    )
    parser.add_argument(
        "--chaos-preset",
        choices=["pr", "full"],
        default="pr",
        help="Chaos stage preset (default: pr).",
    )
    parser.add_argument(
        "--chaos-timeout-seconds",
        type=int,
        default=900,
        help="Timeout for chaos stage (default: 900).",
    )
    parser.add_argument(
        "--soak-seconds",
        type=int,
        default=1800,
        help="Live soak duration in seconds (default: 1800).",
    )
    parser.add_argument(
        "--soak-concurrency",
        type=int,
        default=4,
        help="Concurrent workers for live soak stage (default: 4).",
    )
    parser.add_argument(
        "--skip-live-soak",
        action="store_true",
        help="Skip the long-running live soak stage.",
    )
    parser.add_argument(
        "--release-min-soak-seconds",
        type=int,
        default=900,
        help="Minimum soak duration enforced in release profile (default: 900).",
    )
    parser.add_argument(
        "--release-slo-min-operations",
        type=int,
        default=500,
        help="Release profile minimum operations for soak SLO (default: 500).",
    )
    parser.add_argument(
        "--release-slo-max-failure-rate",
        type=float,
        default=0.01,
        help="Release profile maximum soak failure rate (default: 0.01).",
    )
    parser.add_argument(
        "--release-slo-max-p95-ms",
        type=float,
        default=1500.0,
        help="Release profile maximum soak p95 latency in ms (default: 1500).",
    )
    parser.add_argument(
        "--release-slo-max-consecutive-unhealthy",
        type=int,
        default=2,
        help="Release profile maximum consecutive unhealthy bridge samples (default: 2).",
    )
    parser.add_argument(
        "--allow-deprecation-warning",
        action="store_true",
        help="Allow blocked deprecation warning signatures in live test output.",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=REPO_ROOT / "target" / "live_check_artifacts",
        help="Directory for stage output artifacts (default: target/live_check_artifacts).",
    )
    parser.add_argument(
        "--no-artifacts",
        action="store_true",
        help="Disable writing stage output artifacts.",
    )
    args = parser.parse_args()

    preflight_ok, preflight_data = _preflight()
    if not preflight_ok:
        print(json.dumps({"ok": False, "preflight": preflight_data}, indent=2, sort_keys=True))
        return 1

    env = os.environ.copy()
    env["MAXMCP_RUN_LIVE_E2E"] = "1"
    profile = str(args.profile or "full").strip().lower()
    effective_skip_live_soak = bool(args.skip_live_soak) or profile == "quick"
    effective_skip_extended_check = bool(args.skip_extended_check)
    effective_run_chaos = bool(args.run_chaos) or profile == "release"
    effective_chaos_preset = "full" if profile == "release" else str(args.chaos_preset)
    if profile == "release":
        if args.skip_live_soak:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "preflight": preflight_data,
                        "error": "release profile cannot be used with --skip-live-soak",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
        if effective_skip_extended_check:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "preflight": preflight_data,
                        "error": "release profile cannot be used with --skip-extended-check",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
        if int(args.soak_seconds) < int(args.release_min_soak_seconds):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "preflight": preflight_data,
                        "error": (
                            "release profile requires --soak-seconds >= "
                            f"{int(args.release_min_soak_seconds)}"
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1

    stages: list[tuple[str, list[str], int]] = [
        (
            "maxpylang_check_extended",
            [str(PYTHON), "scripts/maxpylang_check_extended.py", "--json"],
            max(60, int(args.extended_check_timeout_seconds)),
        ),
        (
            "smoke_managed_runtime",
            [str(PYTHON), "scripts/smoke_managed_runtime.py", "--ready-timeout", "30"],
            max(120, args.timeout_seconds),
        ),
        (
            "live_bridge_e2e",
            [str(PYTHON), "-m", "unittest", "-v", "tests/test_live_bridge_e2e.py"],
            max(300, args.timeout_seconds),
        ),
        (
            "queue_soak_synthetic",
            [str(PYTHON), "-m", "unittest", "-v", "tests/test_soak_queue.py"],
            max(120, args.timeout_seconds),
        ),
    ]
    if effective_skip_extended_check:
        stages = [stage for stage in stages if stage[0] != "maxpylang_check_extended"]

    if effective_run_chaos:
        stages.append(
            (
                "chaos_live_bridge",
                [
                    str(PYTHON),
                    "scripts/chaos_live_bridge.py",
                    "--json",
                    "--preset",
                    str(effective_chaos_preset),
                ],
                max(120, int(args.chaos_timeout_seconds)),
            )
        )
    if not effective_skip_live_soak:
        soak_timeout = max(args.timeout_seconds, args.soak_seconds + 300)
        soak_cmd = [
            str(PYTHON),
            "scripts/soak_live_bridge.py",
            "--duration-seconds",
            str(args.soak_seconds),
            "--concurrency",
            str(args.soak_concurrency),
            "--json",
        ]
        if profile == "release":
            soak_cmd.extend(
                [
                    "--enforce-slo",
                    "--slo-min-operations",
                    str(args.release_slo_min_operations),
                    "--slo-max-failure-rate",
                    str(args.release_slo_max_failure_rate),
                    "--slo-max-p95-ms",
                    str(args.release_slo_max_p95_ms),
                    "--slo-max-consecutive-unhealthy",
                    str(args.release_slo_max_consecutive_unhealthy),
                ]
            )
        stages.append(
            (
                "live_bridge_soak",
                soak_cmd,
                soak_timeout,
            )
        )

    results: list[dict] = []
    release_gate_failures: list[str] = []
    release_gate_warnings: list[str] = []
    saw_extended_stage = False
    saw_chaos_stage = False
    for name, cmd, stage_timeout in stages:
        stage = _run_stage(name, cmd, timeout=stage_timeout, env=env)
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
        if name == "maxpylang_check_extended":
            saw_extended_stage = True
            parsed, parse_error = _parse_json_from_output(stage.get("stdout", ""))
            if parsed is not None:
                stage["extended_check_summary"] = {
                    "ok": bool(parsed.get("ok", False)),
                    "summary": parsed.get("summary", {}),
                    "failures": parsed.get("summary", {}).get("failures", []),
                }
                if profile == "release" and not bool(parsed.get("ok", False)):
                    release_gate_failures.append(
                        "release profile requires maxpylang_check_extended to pass."
                    )
            else:
                stage["extended_check_summary_error"] = parse_error
                if profile == "release":
                    release_gate_failures.append(
                        f"release profile requires parseable extended-check JSON ({parse_error})."
                    )
                else:
                    release_gate_warnings.append(
                        f"could not parse extended-check JSON output ({parse_error}); report enrichment skipped."
                    )
        if name == "chaos_live_bridge":
            saw_chaos_stage = True
            parsed, parse_error = _parse_json_from_output(stage.get("stdout", ""))
            if parsed is not None:
                stage["chaos_summary"] = {
                    "ok": bool(parsed.get("ok", False)),
                    "summary": parsed.get("summary", {}),
                    "aggregate_slo": parsed.get("aggregate_slo", {}),
                }
                if profile == "release" and not bool(parsed.get("ok", False)):
                    release_gate_failures.append("release profile chaos gate did not pass.")
            else:
                stage["chaos_summary_error"] = parse_error
                if profile == "release":
                    release_gate_failures.append(
                        f"release profile requires parseable chaos JSON output ({parse_error})."
                    )
                else:
                    release_gate_warnings.append(
                        f"could not parse chaos JSON output ({parse_error}); report enrichment skipped."
                    )
        if name == "live_bridge_soak":
            parsed, parse_error = _parse_json_from_output(stage.get("stdout", ""))
            if parsed is not None:
                stage["soak_summary"] = {
                    "result": parsed.get("result", {}),
                    "slo": parsed.get("slo", {}),
                    "operations_total": parsed.get("operations_total"),
                    "duration_seconds": parsed.get("duration_seconds"),
                }
                if profile == "release":
                    slo = parsed.get("slo", {})
                    if not isinstance(slo, dict):
                        release_gate_failures.append(
                            "release profile requires soak SLO output payload."
                        )
                    elif not bool(slo.get("passed", False)):
                        release_gate_failures.append(
                            "release profile failed soak SLO thresholds."
                        )
                    measurements = slo.get("measurements", {})
                    if isinstance(measurements, dict):
                        operations_total = measurements.get("operations_total")
                        if isinstance(operations_total, (int, float)) and operations_total < int(
                            args.release_slo_min_operations
                        ):
                            release_gate_failures.append(
                                "release profile soak operations below release minimum."
                            )
            else:
                stage["soak_summary_error"] = parse_error
                if profile == "release":
                    release_gate_failures.append(
                        f"release profile requires parseable soak JSON output ({parse_error})."
                    )
                else:
                    release_gate_warnings.append(
                        f"could not parse soak JSON output ({parse_error}); report enrichment skipped."
                    )
        results.append(stage)
        if not args.quiet and not args.json:
            print(f"[{name}] {'PASS' if stage['ok'] else 'FAIL'} ({stage['duration_seconds']}s)")
            if stage["stdout"].strip():
                print(stage["stdout"].rstrip())
            if stage["stderr"].strip():
                print(stage["stderr"].rstrip(), file=sys.stderr)
        if not stage["ok"]:
            break

    if profile == "release" and effective_skip_live_soak:
        release_gate_failures.append("release profile requires live soak stage.")
    if profile == "release" and not saw_extended_stage:
        release_gate_failures.append("release profile requires maxpylang_check_extended stage.")
    if profile == "release" and not saw_chaos_stage:
        release_gate_failures.append("release profile requires chaos_live_bridge stage.")
    if release_gate_failures:
        for stage in results:
            if stage.get("name") in {"live_bridge_soak", "chaos_live_bridge", "maxpylang_check_extended"}:
                stage["ok"] = False
                stage["exit_code"] = stage.get("exit_code") or 1
                stage["stderr"] = (
                    (stage.get("stderr", "") + "\n" + "\n".join(release_gate_failures)).strip()
                )
                break

    summary = {
        "ok": all(r["ok"] for r in results) and not release_gate_failures,
        "profile": profile,
        "preflight": preflight_data,
        "stages": [
            {
                "name": r["name"],
                "ok": r["ok"],
                "exit_code": r["exit_code"],
                "duration_seconds": r["duration_seconds"],
                "extended_check_summary": r.get("extended_check_summary"),
                "extended_check_summary_error": r.get("extended_check_summary_error"),
                "chaos_summary": r.get("chaos_summary"),
                "chaos_summary_error": r.get("chaos_summary_error"),
                "soak_summary": r.get("soak_summary"),
                "soak_summary_error": r.get("soak_summary_error"),
            }
            for r in results
        ],
        "release_gate": {
            "failures": release_gate_failures,
            "warnings": release_gate_warnings,
        },
        "total_duration_seconds": round(sum(r["duration_seconds"] for r in results), 3),
    }
    if not args.no_artifacts:
        artifact_run_dir = _write_artifacts(
            preflight=preflight_data,
            results=results,
            summary=summary,
            artifacts_dir=Path(args.artifacts_dir),
        )
        summary["artifacts_dir"] = str(artifact_run_dir)

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
