#!/usr/bin/env python3
"""Live bridge soak test with bounded failure artifacts."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import random
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import server  # noqa: E402


FATAL_TRANSPORT_MARKERS = (
    "Failed to hand off request through dictionary transport.",
    "Dictionary request transport is currently unhealthy.",
    "Dictionary request transport is required but unavailable",
)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * pct
    lower = int(idx)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    frac = idx - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * frac)


def _json_deepcopy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _safe_now_label() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _sanitize_for_json(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        if isinstance(value, dict):
            return {str(k): _sanitize_for_json(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sanitize_for_json(v) for v in value]
        return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_sanitize_for_json(payload), indent=2, sort_keys=True))


def _copy_recent_crash_reports(
    *,
    destination: Path,
    started_at_epoch: float,
    max_reports: int,
) -> list[str]:
    crash_root = Path("~/Library/Logs/DiagnosticReports").expanduser()
    if not crash_root.exists():
        return []

    candidates: list[Path] = []
    patterns = ("Max*.crash", "Max*.ips", "Max*.diag")
    for pattern in patterns:
        candidates.extend(crash_root.glob(pattern))

    filtered = [
        path
        for path in candidates
        if path.is_file() and path.stat().st_mtime >= (started_at_epoch - 120.0)
    ]
    filtered.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    copied: list[str] = []
    if max_reports <= 0:
        return copied

    destination.mkdir(parents=True, exist_ok=True)
    for path in filtered[:max_reports]:
        target = destination / path.name
        shutil.copy2(path, target)
        copied.append(str(target))
    return copied


def _collect_process_snapshot() -> dict[str, Any]:
    commands = [
        ["ps", "axo", "pid,ppid,etime,command"],
        ["ps", "ax", "-o", "pid,ppid,etime,command"],
        ["ps", "ax", "-o", "pid,ppid,etimes,command"],
    ]
    errors: list[str] = []
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0:
                return {
                    "command": cmd,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "returncode": proc.returncode,
                }
            errors.append(
                f"{cmd}: rc={proc.returncode}, stderr={proc.stderr.strip() or '<empty>'}"
            )
        except Exception as exc:
            errors.append(f"{cmd}: {exc}")
    return {"error": "unable to collect process snapshot", "attempts": errors}


def _compute_soak_slo(
    *,
    stats: dict[str, Any],
    bridge_metrics: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    by_operation = stats.get("by_operation", {})
    operation_latencies: list[float] = []
    failed_ops = 0
    for row in by_operation.values():
        if not isinstance(row, dict):
            continue
        latencies = row.get("latency_ms", [])
        if isinstance(latencies, list):
            for value in latencies:
                if isinstance(value, (int, float)):
                    operation_latencies.append(float(value))
        failed = row.get("failed", 0)
        if isinstance(failed, int):
            failed_ops += failed

    operations_total = int(stats.get("operations_total", 0) or 0)
    failures = max(failed_ops, len(stats.get("errors", [])))
    failure_rate = (failures / operations_total) if operations_total > 0 else 1.0
    p95_latency = _percentile(operation_latencies, 0.95)
    consecutive_unhealthy_max = int(stats.get("consecutive_unhealthy_max", 0) or 0)

    queue_wait_p95 = None
    rolling_failure_rate = None
    rolling_p95_latency = None
    if isinstance(bridge_metrics, dict):
        queue_wait = bridge_metrics.get("queue_wait_ms", {})
        if isinstance(queue_wait, dict):
            queue_wait_p95 = queue_wait.get("p95")
        rolling = bridge_metrics.get("rolling_windows", {})
        if isinstance(rolling, dict):
            rolling_failure_rate = rolling.get("failure_rate")
            rolling_p95_latency = rolling.get("p95_latency_ms")

    thresholds = {
        "min_operations": max(1, int(args.slo_min_operations)),
        "max_failure_rate": max(0.0, float(args.slo_max_failure_rate)),
        "max_p95_latency_ms": max(1.0, float(args.slo_max_p95_ms)),
        "max_consecutive_unhealthy": max(1, int(args.slo_max_consecutive_unhealthy)),
    }
    failures_list: list[dict[str, Any]] = []
    if operations_total < thresholds["min_operations"]:
        failures_list.append(
            {
                "objective": "min_operations",
                "current": operations_total,
                "threshold": thresholds["min_operations"],
                "message": "Soak did not execute enough operations for stable reliability signal.",
            }
        )
    if failure_rate > thresholds["max_failure_rate"]:
        failures_list.append(
            {
                "objective": "max_failure_rate",
                "current": round(failure_rate, 6),
                "threshold": thresholds["max_failure_rate"],
                "message": "Observed failure rate exceeded allowed threshold.",
            }
        )
    if p95_latency is not None and p95_latency > thresholds["max_p95_latency_ms"]:
        failures_list.append(
            {
                "objective": "max_p95_latency_ms",
                "current": round(float(p95_latency), 3),
                "threshold": thresholds["max_p95_latency_ms"],
                "message": "Observed p95 latency exceeded allowed threshold.",
            }
        )
    if consecutive_unhealthy_max > thresholds["max_consecutive_unhealthy"]:
        failures_list.append(
            {
                "objective": "max_consecutive_unhealthy",
                "current": consecutive_unhealthy_max,
                "threshold": thresholds["max_consecutive_unhealthy"],
                "message": "Bridge remained unhealthy for too many consecutive polls.",
            }
        )

    return {
        "passed": len(failures_list) == 0,
        "thresholds": thresholds,
        "measurements": {
            "operations_total": operations_total,
            "failures": failures,
            "failure_rate": round(failure_rate, 6),
            "p95_latency_ms": p95_latency,
            "p95_queue_wait_ms": queue_wait_p95,
            "rolling_failure_rate": rolling_failure_rate,
            "rolling_p95_latency_ms": rolling_p95_latency,
            "consecutive_unhealthy_max": consecutive_unhealthy_max,
        },
        "failures": failures_list,
    }


async def _capture_failure_artifacts(
    *,
    reason: str,
    error: BaseException | None,
    started_at_epoch: float,
    artifacts_root: Path,
    conn: server.MaxMSPConnection,
    runtime: server.MaxRuntimeManager,
    stats: dict[str, Any],
    args: argparse.Namespace,
) -> Path:
    run_dir = artifacts_root / f"soak_failure_{_safe_now_label()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    runtime_status: dict[str, Any]
    try:
        runtime_status = await runtime.collect_status(check_bridge=True)
    except Exception as exc:
        runtime_status = {"error": str(exc), "type": type(exc).__name__}

    metrics_snapshot: dict[str, Any]
    try:
        metrics_snapshot = conn.metrics_snapshot(include_events=True)
    except Exception as exc:
        metrics_snapshot = {"error": str(exc), "type": type(exc).__name__}

    process_snapshot = _collect_process_snapshot()
    crash_report_paths = _copy_recent_crash_reports(
        destination=run_dir / "crash_reports",
        started_at_epoch=started_at_epoch,
        max_reports=max(0, int(args.max_crash_reports)),
    )

    payload = {
        "reason": reason,
        "error_type": type(error).__name__ if error else None,
        "error_message": str(error) if error else None,
        "traceback": (
            "".join(traceback.format_exception(type(error), error, error.__traceback__))
            if error
            else ""
        ),
        "started_at_epoch": started_at_epoch,
        "captured_at_epoch": time.time(),
        "args": vars(args),
        "stats": stats,
        "runtime_status": runtime_status,
        "transport_health": conn.transport_health_snapshot(),
        "metrics_snapshot": metrics_snapshot,
        "process_snapshot_file": "process_snapshot.json",
        "crash_reports": crash_report_paths,
    }
    _write_json(run_dir / "failure_summary.json", payload)
    _write_json(run_dir / "process_snapshot.json", process_snapshot)

    return run_dir


async def _wait_bridge_healthy(
    runtime: server.MaxRuntimeManager,
    *,
    ready_timeout: float,
) -> dict[str, Any]:
    started = time.monotonic()
    while time.monotonic() - started < ready_timeout:
        status = await runtime.collect_status(check_bridge=True)
        if status.get("bridge_connected") and status.get("bridge_healthy"):
            return status
        ping_error = str(status.get("bridge_ping_error") or "")
        if any(marker in ping_error for marker in FATAL_TRANSPORT_MARKERS):
            raise RuntimeError(f"bridge transport unhealthy: {ping_error}")
        await asyncio.sleep(0.5)
    raise RuntimeError(f"bridge health check timed out after {ready_timeout:.1f}s")


async def _op_add_remove_roundtrip(
    conn: server.MaxMSPConnection,
    *,
    worker_id: int,
    sequence: int,
    timeout: float,
) -> None:
    varname = f"soak_obj_w{worker_id}_{sequence}_{int(time.time() * 1000)}"
    x = 80 + ((worker_id % 6) * 120)
    y = 100 + ((sequence % 18) * 24)

    await conn.send_request(
        {
            "action": "add_object",
            "position": [x, y],
            "obj_type": "button",
            "varname": varname,
            "args": [],
        },
        timeout=timeout,
    )
    try:
        await conn.send_request(
            {"action": "send_bang_to_object", "varname": varname},
            timeout=timeout,
        )
    finally:
        await conn.send_request(
            {"action": "remove_object", "varname": varname},
            timeout=timeout,
        )


async def _op_apply_progressive_snapshot(
    runtime: server.MaxRuntimeManager,
    snapshot: dict[str, Any],
    *,
    timeout_seconds: float,
    chunk_size: int,
) -> None:
    result = await runtime._apply_topology_snapshot_progressive(  # noqa: SLF001 - soak-only internal stress
        snapshot,
        timeout_seconds=timeout_seconds,
        chunk_size=chunk_size,
    )
    if not bool(result.get("done", False)):
        raise RuntimeError("progressive topology apply did not complete")


async def _run_soak(args: argparse.Namespace) -> tuple[int, dict[str, Any], Path | None]:
    conn = server.MaxMSPConnection(
        server.SOCKETIO_SERVER_URL,
        server.SOCKETIO_SERVER_PORT,
        server.NAMESPACE,
    )
    runtime = server.MaxRuntimeManager(conn)
    conn.runtime_manager = runtime

    started_at_epoch = time.time()
    started_at_monotonic = time.monotonic()
    deadline = started_at_monotonic + max(1.0, float(args.duration_seconds))
    stop_event = asyncio.Event()
    apply_lock = asyncio.Lock()
    stats: dict[str, Any] = {
        "operations_total": 0,
        "by_operation": {},
        "errors": [],
        "warnings": [],
        "status_samples": [],
        "duration_seconds": 0.0,
        "consecutive_unhealthy_max": 0,
    }
    failure_artifacts_dir: Path | None = None
    first_error: BaseException | None = None

    if args.seed is not None:
        random.seed(int(args.seed))

    async def _record_success(op: str, elapsed_ms: float) -> None:
        stats["operations_total"] += 1
        bucket = stats["by_operation"].setdefault(op, {"ok": 0, "failed": 0, "latency_ms": []})
        bucket["ok"] += 1
        bucket["latency_ms"].append(round(elapsed_ms, 3))

    async def _record_failure(op: str, exc: BaseException) -> None:
        bucket = stats["by_operation"].setdefault(op, {"ok": 0, "failed": 0, "latency_ms": []})
        bucket["failed"] += 1
        stats["errors"].append(
            {
                "operation": op,
                "type": type(exc).__name__,
                "message": str(exc),
                "timestamp_epoch": time.time(),
            }
        )

    async def _worker(worker_id: int) -> None:
        nonlocal first_error
        sequence = 0
        while not stop_event.is_set() and time.monotonic() < deadline:
            sequence += 1
            op = "health_ping"
            try:
                if (
                    args.include_apply
                    and worker_id == 0
                    and sequence % max(1, int(args.apply_every)) == 0
                ):
                    op = "apply_topology_snapshot_progressive"
                else:
                    choice = random.random()
                    if choice < 0.50:
                        op = "health_ping"
                    elif choice < 0.75:
                        op = "get_objects_in_patch"
                    elif choice < 0.95:
                        op = "add_remove_roundtrip"
                    else:
                        op = "capabilities"

                op_started = time.perf_counter()
                if op == "health_ping":
                    await conn.send_request({"action": "health_ping"}, timeout=args.operation_timeout)
                elif op == "get_objects_in_patch":
                    payload = await conn.send_request(
                        {"action": "get_objects_in_patch"},
                        timeout=args.operation_timeout,
                    )
                    if not isinstance(payload, dict):
                        raise RuntimeError("get_objects_in_patch returned non-dict payload")
                elif op == "capabilities":
                    await conn.refresh_capabilities()
                elif op == "add_remove_roundtrip":
                    await _op_add_remove_roundtrip(
                        conn,
                        worker_id=worker_id,
                        sequence=sequence,
                        timeout=args.operation_timeout,
                    )
                else:
                    async with apply_lock:
                        await _op_apply_progressive_snapshot(
                            runtime,
                            baseline_snapshot,
                            timeout_seconds=args.apply_timeout,
                            chunk_size=args.apply_chunk_size,
                        )
                await _record_success(op, (time.perf_counter() - op_started) * 1000.0)
            except Exception as exc:  # pragma: no cover - live path
                await _record_failure(op, exc)
                if first_error is None:
                    first_error = exc
                stop_event.set()
                break

            await asyncio.sleep(max(0.0, float(args.sleep_seconds)))

    async def _monitor() -> None:
        nonlocal first_error
        consecutive_unhealthy = 0
        while not stop_event.is_set() and time.monotonic() < deadline:
            try:
                status = await runtime.collect_status(check_bridge=True)
            except Exception as exc:  # pragma: no cover - live path
                stats["warnings"].append(
                    {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "timestamp_epoch": time.time(),
                    }
                )
                consecutive_unhealthy += 1
                if consecutive_unhealthy >= max(1, int(args.max_consecutive_unhealthy)):
                    if first_error is None:
                        first_error = exc
                    stop_event.set()
                    break
                await asyncio.sleep(max(1.0, float(args.status_interval_seconds)))
                continue

            sample = {
                "timestamp_epoch": time.time(),
                "bridge_connected": bool(status.get("bridge_connected")),
                "bridge_healthy": bool(status.get("bridge_healthy")),
                "bridge_ping_error": status.get("bridge_ping_error"),
                "transport_failure_streak": (
                    status.get("transport_health", {}) or {}
                ).get("failure_streak"),
            }
            stats["status_samples"].append(sample)
            if len(stats["status_samples"]) > 200:
                stats["status_samples"] = stats["status_samples"][-200:]

            ping_error = str(status.get("bridge_ping_error") or "")
            unhealthy = not sample["bridge_connected"] or not sample["bridge_healthy"]
            marker_hit = any(marker in ping_error for marker in FATAL_TRANSPORT_MARKERS)
            if unhealthy or marker_hit:
                consecutive_unhealthy += 1
                stats["consecutive_unhealthy_max"] = max(
                    int(stats.get("consecutive_unhealthy_max", 0)),
                    consecutive_unhealthy,
                )
                if marker_hit or consecutive_unhealthy >= max(1, int(args.max_consecutive_unhealthy)):
                    if first_error is None:
                        first_error = RuntimeError(
                            f"bridge unhealthy during soak: {ping_error or 'no ping payload'}"
                        )
                    stop_event.set()
                    break
            else:
                consecutive_unhealthy = 0

            await asyncio.sleep(max(1.0, float(args.status_interval_seconds)))

    try:
        ready = await runtime.ensure_runtime_ready()
        if not ready.get("ready"):
            raise RuntimeError(f"runtime not ready: {ready.get('error')}")
        await _wait_bridge_healthy(runtime, ready_timeout=args.ready_timeout)

        raw_topology = await conn.send_request(
            {"action": "get_objects_in_patch"},
            timeout=max(2.0, float(args.operation_timeout)),
        )
        if not isinstance(raw_topology, dict):
            raise RuntimeError("failed to capture baseline topology for soak")

        baseline_snapshot = {
            "boxes": _json_deepcopy(raw_topology.get("boxes", [])),
            "lines": _json_deepcopy(raw_topology.get("lines", [])),
        }

        workers = [
            asyncio.create_task(_worker(worker_idx))
            for worker_idx in range(max(1, int(args.concurrency)))
        ]
        monitor_task = asyncio.create_task(_monitor())

        await asyncio.wait(workers, return_when=asyncio.ALL_COMPLETED)
        stop_event.set()
        await monitor_task
    except Exception as exc:  # pragma: no cover - live path
        if first_error is None:
            first_error = exc
    finally:
        stats["duration_seconds"] = round(time.monotonic() - started_at_monotonic, 3)
        try:
            bridge_metrics = conn.metrics_snapshot(include_events=False)
        except Exception as exc:
            bridge_metrics = {"error": str(exc), "type": type(exc).__name__}
        stats["bridge_metrics"] = bridge_metrics
        stats["transport_health"] = conn.transport_health_snapshot()
        stats["slo"] = _compute_soak_slo(
            stats=stats,
            bridge_metrics=bridge_metrics if isinstance(bridge_metrics, dict) else {},
            args=args,
        )
        if first_error is None and bool(args.enforce_slo) and not stats["slo"]["passed"]:
            first_error = RuntimeError(
                f"SLO gate failed with {len(stats['slo'].get('failures', []))} breach(es)."
            )
        if first_error is not None:
            failure_artifacts_dir = await _capture_failure_artifacts(
                reason="live_soak_failed",
                error=first_error,
                started_at_epoch=started_at_epoch,
                artifacts_root=Path(args.failure_artifacts_dir),
                conn=conn,
                runtime=runtime,
                stats=copy.deepcopy(stats),
                args=args,
            )
        await conn.disconnect()

    if first_error is not None:
        stats["result"] = {
            "ok": False,
            "error_type": type(first_error).__name__,
            "error_message": str(first_error),
            "failure_artifacts_dir": str(failure_artifacts_dir) if failure_artifacts_dir else None,
        }
        return 1, stats, failure_artifacts_dir

    stats["result"] = {"ok": True}
    return 0, stats, None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live managed-bridge soak test.")
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=1800.0,
        help="Total soak duration in seconds (default: 1800).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Number of concurrent worker loops (default: 4).",
    )
    parser.add_argument(
        "--operation-timeout",
        type=float,
        default=8.0,
        help="Per-request timeout for light operations (default: 8.0).",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for initial bridge readiness (default: 30.0).",
    )
    parser.add_argument(
        "--status-interval-seconds",
        type=float,
        default=10.0,
        help="Health polling interval during soak (default: 10.0).",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.02,
        help="Inter-operation sleep for each worker (default: 0.02).",
    )
    parser.add_argument(
        "--include-apply",
        dest="include_apply",
        action="store_true",
        default=True,
        help="Include periodic progressive topology apply operations (default: enabled).",
    )
    parser.add_argument(
        "--no-apply",
        dest="include_apply",
        action="store_false",
        help="Disable progressive topology apply operations during soak.",
    )
    parser.add_argument(
        "--apply-every",
        type=int,
        default=120,
        help="Worker-0 interval for progressive apply operation (default: 120).",
    )
    parser.add_argument(
        "--apply-timeout",
        type=float,
        default=25.0,
        help="Total timeout budget for each progressive apply attempt (default: 25.0).",
    )
    parser.add_argument(
        "--apply-chunk-size",
        type=int,
        default=64,
        help="Chunk size for progressive apply operations (default: 64).",
    )
    parser.add_argument(
        "--max-consecutive-unhealthy",
        type=int,
        default=3,
        help="Fail soak after this many consecutive unhealthy status polls (default: 3).",
    )
    parser.add_argument(
        "--failure-artifacts-dir",
        type=Path,
        default=REPO_ROOT / "target" / "live_soak_artifacts",
        help="Directory for failure artifacts (default: target/live_soak_artifacts).",
    )
    parser.add_argument(
        "--max-crash-reports",
        type=int,
        default=5,
        help="Max crash reports to copy into failure artifacts (default: 5).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for deterministic operation mix.",
    )
    parser.add_argument(
        "--enforce-slo",
        action="store_true",
        help="Fail soak when computed reliability SLOs are not met.",
    )
    parser.add_argument(
        "--slo-min-operations",
        type=int,
        default=200,
        help="Minimum operations required to consider soak signal valid (default: 200).",
    )
    parser.add_argument(
        "--slo-max-failure-rate",
        type=float,
        default=0.02,
        help="Maximum allowed operation failure rate (default: 0.02).",
    )
    parser.add_argument(
        "--slo-max-p95-ms",
        type=float,
        default=2000.0,
        help="Maximum allowed p95 operation latency in ms (default: 2000).",
    )
    parser.add_argument(
        "--slo-max-consecutive-unhealthy",
        type=int,
        default=3,
        help="Maximum allowed consecutive unhealthy status polls (default: 3).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON summary only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    exit_code, stats, _ = asyncio.run(_run_soak(args))
    if args.json:
        print(json.dumps(_sanitize_for_json(stats), indent=2, sort_keys=True))
        return exit_code

    print("[live_bridge_soak]")
    print(json.dumps(_sanitize_for_json(stats), indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
