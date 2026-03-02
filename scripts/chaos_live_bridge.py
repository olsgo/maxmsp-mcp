#!/usr/bin/env python3
"""Run deterministic chaos scenarios against a live Max bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import server


def _safe_now_label() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(0.95 * (len(ordered) - 1))))
    return float(ordered[idx])


def _preset_thresholds(preset: str) -> dict[str, float]:
    if preset == "full":
        return {
            "min_operations": 80.0,
            "max_failure_rate": 0.03,
            "max_p95_latency_ms": 1800.0,
            "max_consecutive_unhealthy": 3.0,
        }
    return {
        "min_operations": 30.0,
        "max_failure_rate": 0.08,
        "max_p95_latency_ms": 2500.0,
        "max_consecutive_unhealthy": 4.0,
    }


async def _run_bridge_op(
    label: str,
    fn: Callable[[], Awaitable[Any]],
) -> dict:
    started = time.perf_counter()
    try:
        await fn()
        ok = True
        error = None
    except Exception as exc:  # pragma: no cover - exercised in live mode only
        ok = False
        error = str(exc)
    return {
        "label": label,
        "ok": ok,
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "error": error,
    }


async def _scenario_disconnect_reconnect(
    conn: server.MaxMSPConnection,
    *,
    iterations: int,
) -> dict:
    ops: list[dict] = []
    for idx in range(iterations):
        ops.append(
            await _run_bridge_op(
                f"disconnect_reconnect:pre_ping:{idx}",
                lambda: conn.send_request({"action": "health_ping"}, timeout=3.0),
            )
        )
        await conn.disconnect()
        await asyncio.sleep(0.15)
        ops.append(
            await _run_bridge_op(
                f"disconnect_reconnect:reconnect:{idx}",
                lambda: conn.ensure_connected(retries=3, retry_delay=0.4),
            )
        )
        ops.append(
            await _run_bridge_op(
                f"disconnect_reconnect:post_ping:{idx}",
                lambda: conn.send_request({"action": "health_ping"}, timeout=3.0),
            )
        )
    failures = [row for row in ops if not row["ok"]]
    return {
        "name": "disconnect_reconnect_burst",
        "passed": len(failures) == 0,
        "operations_total": len(ops),
        "failures_total": len(failures),
        "latencies_ms": [row["latency_ms"] for row in ops],
        "max_consecutive_unhealthy": 1 if failures else 0,
        "failure_messages": [row["error"] for row in failures if row.get("error")],
    }


async def _scenario_bridge_response_stall(conn: server.MaxMSPConnection) -> dict:
    ops: list[dict] = []
    unhealthy_streak = 0
    max_unhealthy_streak = 0

    # Intentionally use an unrealistically small timeout once to validate recovery.
    timeout_probe = await _run_bridge_op(
        "bridge_response_stall:induced_timeout",
        lambda: conn.send_request({"action": "health_ping"}, timeout=0.001),
    )
    ops.append(timeout_probe)
    if timeout_probe["ok"]:
        timeout_probe["ok"] = False
        timeout_probe["error"] = "expected induced timeout but request succeeded"

    recovered = False
    for idx in range(6):
        probe = await _run_bridge_op(
            f"bridge_response_stall:recovery_ping:{idx}",
            lambda: conn.send_request({"action": "health_ping"}, timeout=3.0),
        )
        ops.append(probe)
        if probe["ok"]:
            recovered = True
            unhealthy_streak = 0
            break
        unhealthy_streak += 1
        max_unhealthy_streak = max(max_unhealthy_streak, unhealthy_streak)
        await asyncio.sleep(0.2)

    failures = [row for row in ops if not row["ok"]]
    passed = recovered and bool(failures)
    if not recovered:
        failures.append({"error": "bridge did not recover from induced timeout"})
    return {
        "name": "bridge_response_stall",
        "passed": passed,
        "operations_total": len(ops),
        "failures_total": len([row for row in ops if not row["ok"]]),
        "latencies_ms": [row["latency_ms"] for row in ops],
        "max_consecutive_unhealthy": max_unhealthy_streak,
        "failure_messages": [row["error"] for row in failures if row.get("error")],
    }


def _scenario_malformed_envelope_burst(conn: server.MaxMSPConnection) -> dict:
    payloads: list[Any] = [
        None,
        {"request_id": "missing_fields"},
        {"protocol_version": 2, "request_id": "bad_proto_type", "state": "succeeded"},
        {"protocol_version": conn.protocol_version, "request_id": "bad_state", "state": 12},
        {"protocol_version": conn.protocol_version, "request_id": 7, "state": "succeeded"},
    ]
    failures: list[str] = []
    for idx, payload in enumerate(payloads):
        normalized = conn._normalize_response(payload)  # noqa: SLF001 - intentional protocol fuzzing
        error = normalized.get("error") if isinstance(normalized, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        state = normalized.get("state") if isinstance(normalized, dict) else None
        if state != "failed" or code not in {
            server.ERROR_PROTO_V3_MISSING_FIELD,
            server.ERROR_PROTO_V3_INVALID_TYPE,
            server.ERROR_PROTO_V3_UNSUPPORTED_VERSION,
        }:
            failures.append(f"malformed payload {idx} was not rejected with strict-v3 error code")

    return {
        "name": "malformed_envelope_burst",
        "passed": len(failures) == 0,
        "operations_total": len(payloads),
        "failures_total": len(failures),
        "latencies_ms": [0.0 for _ in payloads],
        "max_consecutive_unhealthy": 0,
        "failure_messages": failures,
    }


async def _scenario_backpressure_spike(
    conn: server.MaxMSPConnection,
    *,
    object_count: int,
    concurrency: int,
) -> dict:
    ops: list[dict] = []
    created: set[str] = set()
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def guarded(label: str, fn: Callable[[], Awaitable[Any]]) -> dict:
        async with sem:
            return await _run_bridge_op(label, fn)

    avoid_rect = await conn.send_request({"action": "get_avoid_rect_position"}, timeout=8.0)
    right = avoid_rect[2] if isinstance(avoid_rect, list) and len(avoid_rect) > 2 else 240
    top = avoid_rect[1] if isinstance(avoid_rect, list) and len(avoid_rect) > 1 else 120
    base_x = int(right) + 40
    base_y = int(top) + 40

    varnames = [f"chaos_bp_{int(time.time())}_{idx}" for idx in range(object_count)]

    async def add_one(idx: int, varname: str) -> dict:
        async def _add() -> Any:
            result = await conn.send_request(
                {
                    "action": "add_object",
                    "position": [base_x + (idx % 8) * 28, base_y + (idx // 8) * 22],
                    "obj_type": "button",
                    "args": [],
                    "varname": varname,
                },
                timeout=12.0,
            )
            created.add(varname)
            return result

        return await guarded(f"backpressure:add:{idx}", _add)

    async def remove_one(idx: int, varname: str) -> dict:
        async def _remove() -> Any:
            result = await conn.send_request(
                {"action": "remove_object", "varname": varname},
                timeout=8.0,
            )
            created.discard(varname)
            return result

        return await guarded(f"backpressure:remove:{idx}", _remove)

    try:
        add_rows = await asyncio.gather(*(add_one(i, name) for i, name in enumerate(varnames)))
        ops.extend(add_rows)
        remove_rows = await asyncio.gather(*(remove_one(i, name) for i, name in enumerate(varnames)))
        ops.extend(remove_rows)
    finally:
        for leftover in list(created):
            try:
                await conn.send_request({"action": "remove_object", "varname": leftover}, timeout=6.0)
            except Exception:
                pass

    failures = [row for row in ops if not row["ok"]]
    return {
        "name": "backpressure_spike",
        "passed": len(failures) == 0,
        "operations_total": len(ops),
        "failures_total": len(failures),
        "latencies_ms": [row["latency_ms"] for row in ops],
        "max_consecutive_unhealthy": 1 if failures else 0,
        "failure_messages": [row["error"] for row in failures if row.get("error")],
    }


def _compute_aggregate_slo(
    *,
    scenario_results: list[dict],
    targets: dict[str, float],
) -> dict:
    operations_total = int(sum(int(row.get("operations_total", 0)) for row in scenario_results))
    failures_total = int(sum(int(row.get("failures_total", 0)) for row in scenario_results))
    all_latencies: list[float] = []
    max_consecutive_unhealthy = 0
    for row in scenario_results:
        latencies = row.get("latencies_ms", [])
        if isinstance(latencies, list):
            all_latencies.extend(float(value) for value in latencies if isinstance(value, (int, float)))
        max_consecutive_unhealthy = max(
            max_consecutive_unhealthy,
            int(row.get("max_consecutive_unhealthy", 0) or 0),
        )
    failure_rate = float(failures_total) / float(operations_total) if operations_total > 0 else 1.0
    p95_latency_ms = _p95(all_latencies)

    failures: list[str] = []
    if operations_total < int(targets["min_operations"]):
        failures.append(
            f"operations_total={operations_total} below minimum {int(targets['min_operations'])}"
        )
    if failure_rate > float(targets["max_failure_rate"]):
        failures.append(
            f"failure_rate={failure_rate:.4f} exceeds max {float(targets['max_failure_rate']):.4f}"
        )
    if p95_latency_ms > float(targets["max_p95_latency_ms"]):
        failures.append(
            f"p95_latency_ms={p95_latency_ms:.3f} exceeds max {float(targets['max_p95_latency_ms']):.3f}"
        )
    if max_consecutive_unhealthy > int(targets["max_consecutive_unhealthy"]):
        failures.append(
            "max_consecutive_unhealthy="
            f"{max_consecutive_unhealthy} exceeds max {int(targets['max_consecutive_unhealthy'])}"
        )

    return {
        "targets": targets,
        "measurements": {
            "operations_total": operations_total,
            "failures_total": failures_total,
            "failure_rate": round(failure_rate, 6),
            "p95_latency_ms": round(p95_latency_ms, 3),
            "max_consecutive_unhealthy": max_consecutive_unhealthy,
        },
        "failures": failures,
        "passed": len(failures) == 0,
    }


async def _run(args: argparse.Namespace) -> dict:
    if args.dry_run:
        targets = _preset_thresholds(args.preset)
        scenario_results = [
            {
                "name": "disconnect_reconnect_burst",
                "passed": True,
                "operations_total": 12,
                "failures_total": 0,
                "latencies_ms": [20.0, 25.0, 30.0],
                "max_consecutive_unhealthy": 0,
                "failure_messages": [],
            },
            {
                "name": "bridge_response_stall",
                "passed": True,
                "operations_total": 6,
                "failures_total": 1,
                "latencies_ms": [5.0, 10.0, 12.0],
                "max_consecutive_unhealthy": 1,
                "failure_messages": [],
            },
            {
                "name": "malformed_envelope_burst",
                "passed": True,
                "operations_total": 5,
                "failures_total": 0,
                "latencies_ms": [0.0, 0.0, 0.0],
                "max_consecutive_unhealthy": 0,
                "failure_messages": [],
            },
            {
                "name": "backpressure_spike",
                "passed": True,
                "operations_total": 24,
                "failures_total": 0,
                "latencies_ms": [40.0, 50.0, 65.0],
                "max_consecutive_unhealthy": 0,
                "failure_messages": [],
            },
        ]
        aggregate_slo = _compute_aggregate_slo(scenario_results=scenario_results, targets=targets)
        summary = {
            "passed": bool(aggregate_slo.get("passed", False)),
            "scenario_count": len(scenario_results),
            "scenario_failures": [],
            "failures": list(aggregate_slo.get("failures", [])),
            "duration_seconds": 0.0,
        }
        return {
            "ok": bool(summary["passed"]),
            "preset": args.preset,
            "summary": summary,
            "aggregate_slo": aggregate_slo,
            "scenario_results": scenario_results,
            "artifacts": {},
        }

    conn = server.MaxMSPConnection(server.SOCKETIO_SERVER_URL, server.SOCKETIO_SERVER_PORT, server.NAMESPACE)
    runtime = server.MaxRuntimeManager(conn)
    conn.runtime_manager = runtime

    started = time.perf_counter()
    try:
        readiness = await runtime.ensure_runtime_ready()
        if not readiness.get("ready"):
            return {
                "ok": False,
                "preset": args.preset,
                "summary": {
                    "passed": False,
                    "scenario_count": 0,
                    "scenario_failures": [],
                    "failures": [f"runtime not ready: {readiness.get('error', 'unknown error')}"],
                    "duration_seconds": round(time.perf_counter() - started, 3),
                },
                "aggregate_slo": {"targets": {}, "measurements": {}, "failures": [], "passed": False},
                "scenario_results": [],
                "artifacts": {},
            }

        await conn.ensure_connected(retries=3, retry_delay=0.4)
        targets = _preset_thresholds(args.preset)
        if args.slo_min_operations is not None:
            targets["min_operations"] = float(max(1, int(args.slo_min_operations)))
        if args.slo_max_failure_rate is not None:
            targets["max_failure_rate"] = float(max(0.0, args.slo_max_failure_rate))
        if args.slo_max_p95_ms is not None:
            targets["max_p95_latency_ms"] = float(max(1.0, args.slo_max_p95_ms))
        if args.slo_max_consecutive_unhealthy is not None:
            targets["max_consecutive_unhealthy"] = float(max(1, int(args.slo_max_consecutive_unhealthy)))

        scenario_results: list[dict] = []
        scenario_results.append(
            await _scenario_disconnect_reconnect(
                conn,
                iterations=6 if args.preset == "full" else 3,
            )
        )
        scenario_results.append(await _scenario_bridge_response_stall(conn))
        scenario_results.append(_scenario_malformed_envelope_burst(conn))
        scenario_results.append(
            await _scenario_backpressure_spike(
                conn,
                object_count=20 if args.preset == "full" else 10,
                concurrency=8 if args.preset == "full" else 4,
            )
        )
        aggregate_slo = _compute_aggregate_slo(scenario_results=scenario_results, targets=targets)
        scenario_failures = [
            row.get("name", "unknown")
            for row in scenario_results
            if not bool(row.get("passed", False))
        ]
        failures = list(aggregate_slo.get("failures", []))
        if scenario_failures:
            failures.append(f"scenario failures: {', '.join(scenario_failures)}")
        summary = {
            "passed": len(failures) == 0,
            "scenario_count": len(scenario_results),
            "scenario_failures": scenario_failures,
            "failures": failures,
            "duration_seconds": round(time.perf_counter() - started, 3),
        }
        return {
            "ok": bool(summary["passed"]),
            "preset": args.preset,
            "summary": summary,
            "aggregate_slo": aggregate_slo,
            "scenario_results": scenario_results,
            "artifacts": {},
        }
    finally:
        await conn.disconnect()


def _write_artifacts(payload: dict, artifacts_dir: Path) -> dict:
    run_dir = artifacts_dir / f"chaos_run_{_safe_now_label()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "chaos_summary.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "report_path": str(report_path),
        "run_dir": str(run_dir),
        "size_bytes": report_path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run chaos/fault-injection validation against live bridge.")
    parser.add_argument("--preset", choices=["pr", "full"], default="pr", help="Scenario preset.")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary.")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-JSON output.")
    parser.add_argument("--dry-run", action="store_true", help="Emit deterministic payload without runtime calls.")
    parser.add_argument("--no-artifacts", action="store_true", help="Disable artifact writing.")
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=REPO_ROOT / "target" / "chaos_artifacts",
        help="Directory for chaos artifacts (default: target/chaos_artifacts).",
    )
    parser.add_argument("--slo-min-operations", type=int, default=None)
    parser.add_argument("--slo-max-failure-rate", type=float, default=None)
    parser.add_argument("--slo-max-p95-ms", type=float, default=None)
    parser.add_argument("--slo-max-consecutive-unhealthy", type=int, default=None)
    args = parser.parse_args()

    payload = asyncio.run(_run(args))
    if not args.no_artifacts:
        payload["artifacts"] = _write_artifacts(payload, Path(args.artifacts_dir))

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif not args.quiet:
        print("[chaos_live_bridge]")
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, sort_keys=True))

    return 0 if bool(payload.get("ok", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
