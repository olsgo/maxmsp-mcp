from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any


FAST_ATTACH = "fast_attach"
STRICT_READY = "strict_ready"


def current_connection_epoch(runtime: Any) -> int:
    try:
        return int(getattr(runtime.maxmsp, "connection_epoch", 0))
    except Exception:
        return 0


def compute_ready(runtime: Any, status: dict[str, Any]) -> bool:
    bridge_connected = bool(status.get("bridge_connected"))
    node_hello_ready = bool(runtime.maxmsp.node_hello_seen if runtime.managed_mode else True)
    if runtime.require_healthy_ready:
        return bool(bridge_connected and status.get("bridge_healthy") and node_hello_ready)
    return bool(bridge_connected and node_hello_ready)


def apply_startup_metadata(
    runtime: Any,
    status: dict[str, Any],
    *,
    attach_ready: bool | None = None,
) -> dict[str, Any]:
    payload = dict(status)
    epoch = current_connection_epoch(runtime)
    task = getattr(runtime, "_warmup_task", None)
    task_epoch = int(getattr(runtime, "_warmup_task_epoch", 0) or 0)
    completed_epoch = int(getattr(runtime, "_warmup_completed_epoch", 0) or 0)
    task_matches_epoch = bool(task is not None and task_epoch == epoch)

    payload["startup_mode"] = getattr(runtime, "startup_mode", FAST_ATTACH)
    payload["attach_ready"] = bool(
        payload.get("bridge_connected") if attach_ready is None else attach_ready
    )
    payload["warmup_epoch"] = epoch
    payload["warmup_in_progress"] = bool(task_matches_epoch and not task.done())
    payload["warmup_ready"] = bool(
        getattr(runtime, "_warmup_ready", False) and completed_epoch == epoch
    )
    payload["warmup_error"] = (
        getattr(runtime, "_warmup_error", None)
        if task_matches_epoch or completed_epoch == epoch
        else None
    )
    payload["last_warmup_started_at"] = (
        getattr(runtime, "_warmup_started_at", None)
        if task_matches_epoch or completed_epoch == epoch
        else None
    )
    payload["last_warmup_completed_at"] = (
        getattr(runtime, "_warmup_completed_at", None)
        if completed_epoch == epoch
        else None
    )
    return payload


async def _run_runtime_warmup(
    runtime: Any,
    *,
    checkpoint_journal: dict[str, Any] | None = None,
    host_patch: Path | None = None,
) -> dict[str, Any]:
    runtime._warmup_started_at = time.time()
    runtime._warmup_completed_at = None
    runtime._warmup_ready = False
    runtime._warmup_error = None
    runtime._warmup_status = {}

    status: dict[str, Any] | None = None
    try:
        if not getattr(runtime.maxmsp, "capabilities", {}):
            try:
                await runtime.maxmsp.refresh_capabilities()
            except Exception as exc:
                runtime.maxmsp.last_connect_error = str(exc)

        if runtime.managed_mode and not runtime.maxmsp.node_hello_seen:
            await runtime._wait_for_node_hello(timeout_seconds=5.0)

        workspace_apply_error = None
        try:
            await runtime._apply_target_to_bridge()
        except Exception as exc:
            workspace_apply_error = str(exc)

        status = await runtime.collect_status(check_bridge=True)
        status["checkpoint_journal"] = (
            checkpoint_journal
            if checkpoint_journal is not None
            else await asyncio.to_thread(runtime._load_checkpoint_journal_sync)
        )
        status["node_hello_required"] = bool(runtime.managed_mode)
        status["node_hello_ready"] = bool(
            runtime.maxmsp.node_hello_seen if runtime.managed_mode else True
        )
        status["ready"] = compute_ready(runtime, status)

        recovery_attempt = {
            "attempted": False,
            "mode": None,
            "reconnected": False,
            "error": None,
        }
        if (
            runtime.require_healthy_ready
            and status.get("bridge_connected")
            and not status.get("bridge_healthy")
        ):
            recovery_attempt["attempted"] = True
            recovery_attempt["mode"] = "socket_reconnect"
            try:
                await runtime.maxmsp.disconnect()
                reconnected = await runtime.maxmsp.start_server()
                recovery_attempt["reconnected"] = bool(reconnected)
                if reconnected:
                    status_after_reconnect = await runtime.collect_status(check_bridge=True)
                    status.update(status_after_reconnect)
                    status["node_hello_required"] = bool(runtime.managed_mode)
                    status["node_hello_ready"] = bool(
                        runtime.maxmsp.node_hello_seen if runtime.managed_mode else True
                    )
                    status["ready"] = compute_ready(runtime, status)
            except Exception as exc:
                recovery_attempt["error"] = str(exc)
        if (
            runtime.require_healthy_ready
            and status.get("bridge_connected")
            and not status.get("ready")
            and runtime._is_transport_handoff_failure_status(status)
        ):
            managed_recovery = await runtime._recover_managed_bridge_runtime(
                host_patch=host_patch or runtime._resolve_host_patch(),
                reason=str(
                    status.get("bridge_ping_error")
                    or status.get("error")
                    or "transport handoff failure"
                ),
            )
            recovery_attempt["attempted"] = True
            recovery_attempt["mode"] = "managed_restart"
            recovery_attempt["managed_restart"] = managed_recovery
            if managed_recovery.get("error"):
                recovery_attempt["error"] = managed_recovery.get("error")
            status_after_restart = await runtime.collect_status(check_bridge=True)
            status.update(status_after_restart)
            status["node_hello_required"] = bool(runtime.managed_mode)
            status["node_hello_ready"] = bool(
                runtime.maxmsp.node_hello_seen if runtime.managed_mode else True
            )
            status["ready"] = compute_ready(runtime, status)
        if recovery_attempt["attempted"]:
            status["recovery_attempt"] = recovery_attempt

        if not status["ready"]:
            if status.get("bridge_connected") and not status.get("bridge_healthy"):
                status["error"] = (
                    status.get("bridge_ping_error")
                    or status.get("workspace_status_error")
                    or "Bridge is connected but unhealthy."
                )
            elif status.get("bridge_connected") and not status.get("node_hello_ready"):
                status["error"] = (
                    "Bridge connected but node runtime hello handshake is missing. "
                    "Managed restart may be required."
                )
            else:
                status["error"] = runtime.maxmsp._offline_error_message()
        if workspace_apply_error:
            status["workspace_apply_error"] = workspace_apply_error

        if status["ready"]:
            status["twin_sync"] = await runtime.sync_patch_twin(reason="startup_warmup")
            if runtime.hygiene_manager is not None:
                startup_gc = await runtime.hygiene_manager.run_startup_cleanup_once()
                if startup_gc:
                    status["hygiene_startup_cleanup"] = startup_gc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            status = await runtime.collect_status(check_bridge=False)
        except Exception:
            status = {}
        status["ready"] = False
        status["error"] = str(exc)

    assert status is not None
    final_epoch = current_connection_epoch(runtime)
    runtime._warmup_task_epoch = final_epoch
    runtime._warmup_completed_epoch = final_epoch
    runtime._warmup_ready = bool(status.get("ready"))
    runtime._warmup_error = None if status.get("ready") else status.get("error")
    runtime._warmup_completed_at = time.time()
    runtime._warmup_status = dict(status)
    status = apply_startup_metadata(
        runtime,
        status,
        attach_ready=bool(status.get("bridge_connected")),
    )
    runtime._write_state(status)
    return status


async def schedule_runtime_warmup(
    runtime: Any,
    *,
    checkpoint_journal: dict[str, Any] | None = None,
    host_patch: Path | None = None,
    wait: bool,
    force: bool = False,
) -> dict[str, Any] | None:
    epoch = current_connection_epoch(runtime)
    task_to_await = None
    cached_status = None

    async with runtime._warmup_lock:
        existing = getattr(runtime, "_warmup_task", None)
        if (
            existing is not None
            and runtime._warmup_task_epoch != epoch
            and not existing.done()
        ):
            existing.cancel()
            runtime._warmup_task = None
            existing = None

        if (
            existing is not None
            and runtime._warmup_task_epoch == epoch
            and not existing.done()
        ):
            task_to_await = existing
        elif (
            not force
            and runtime._warmup_completed_epoch == epoch
            and runtime._warmup_ready
        ):
            cached_status = apply_startup_metadata(
                runtime,
                dict(runtime._warmup_status or {}),
                attach_ready=bool(runtime.maxmsp.sio.connected),
            )
        elif (
            not wait
            and not force
            and runtime._warmup_completed_epoch == epoch
            and not runtime._warmup_ready
        ):
            cached_status = apply_startup_metadata(
                runtime,
                dict(runtime._warmup_status or {}),
                attach_ready=bool(runtime.maxmsp.sio.connected),
            )
        else:
            runtime._warmup_task_epoch = epoch
            runtime._warmup_task = asyncio.create_task(
                _run_runtime_warmup(
                    runtime,
                    checkpoint_journal=checkpoint_journal,
                    host_patch=host_patch,
                )
            )
            task_to_await = runtime._warmup_task

    if cached_status is not None:
        return cached_status if wait else None
    if task_to_await is None:
        return None
    if wait:
        return await task_to_await
    return None
