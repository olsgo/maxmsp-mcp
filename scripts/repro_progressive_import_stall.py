#!/usr/bin/env python3
"""Reproduce progressive import stalls and report the first hanging candidate."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import server  # noqa: E402

logging.getLogger().setLevel(logging.WARNING)


def _load_snapshot(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    extracted = server.MaxRuntimeManager._extract_topology_with_format(payload)  # noqa: SLF001
    if extracted:
        _, topology = extracted
        return topology
    raise ValueError(
        f"Unsupported patch payload at {path}. Expected top-level boxes/lines or patcher.boxes/patcher.lines."
    )


def _box_summary(row: Any) -> dict[str, Any]:
    box = row.get("box", row) if isinstance(row, dict) else {}
    if not isinstance(box, dict):
        return {"valid": False}
    return {
        "valid": True,
        "id": box.get("id"),
        "varname": box.get("varname"),
        "maxclass": box.get("maxclass"),
        "text": box.get("text"),
        "boxtext": box.get("boxtext"),
    }


def _line_summary(row: Any) -> dict[str, Any]:
    line = row.get("patchline", row) if isinstance(row, dict) else {}
    if not isinstance(line, dict):
        return {"valid": False}
    return {
        "valid": True,
        "source": line.get("source"),
        "destination": line.get("destination"),
    }


def _candidate_from_state(snapshot: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    boxes = snapshot.get("boxes", [])
    lines = snapshot.get("lines", [])
    if not isinstance(boxes, list):
        boxes = []
    if not isinstance(lines, list):
        lines = []

    if state is None:
        phase = "boxes"
        box_index = 0
        line_index = 0
    else:
        phase = str(state.get("phase", "boxes"))
        box_index = int(state.get("box_index", 0))
        line_index = int(state.get("line_index", 0))

    if phase == "lines":
        row = lines[line_index] if 0 <= line_index < len(lines) else None
        return {
            "phase": "lines",
            "index": line_index,
            "total": len(lines),
            "candidate": _line_summary(row) if row is not None else {"valid": False},
        }

    row = boxes[box_index] if 0 <= box_index < len(boxes) else None
    return {
        "phase": "boxes",
        "index": box_index,
        "total": len(boxes),
        "candidate": _box_summary(row) if row is not None else {"valid": False},
    }


def _exc_to_dict(exc: Exception) -> dict[str, Any]:
    out: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, server.MaxMCPError):
        out["code"] = exc.code
        out["recoverable"] = exc.recoverable
        out["details"] = exc.details
    return out


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    patch_path = Path(args.patch).expanduser().resolve()
    snapshot = _load_snapshot(patch_path)
    total_operations = len(snapshot.get("boxes", [])) + len(snapshot.get("lines", []))

    workspace_varname = args.workspace_varname or f"repro_import_{int(time.time())}"
    max_calls = args.max_calls or max(8, total_operations + 16)

    conn = server.MaxMSPConnection(
        server.SOCKETIO_SERVER_URL,
        server.SOCKETIO_SERVER_PORT,
        server.NAMESPACE,
    )
    runtime = server.MaxRuntimeManager(conn)
    conn.runtime_manager = runtime

    state: dict[str, Any] | None = None
    created_workspace = False
    chunks: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "ok": False,
        "patch": str(patch_path),
        "workspace_varname": workspace_varname,
        "total_boxes": len(snapshot.get("boxes", [])),
        "total_lines": len(snapshot.get("lines", [])),
        "total_operations": total_operations,
        "chunk_size": args.chunk_size,
        "timeout_seconds": args.timeout_seconds,
        "max_calls": max_calls,
        "chunks": chunks,
    }

    try:
        ready = await runtime.ensure_runtime_ready()
        result["runtime_ready"] = bool(ready.get("ready"))

        switch = await conn.send_request(
            {
                "action": "set_workspace_target",
                "target_id": "scratch",
                "workspace_varname": workspace_varname,
                "workspace_name": "progressive import repro",
            },
            timeout=10.0,
        )
        created_workspace = bool(switch.get("created_workspace")) if isinstance(switch, dict) else False
        result["switch"] = switch

        for call_idx in range(1, max_calls + 1):
            candidate = _candidate_from_state(snapshot, state)
            label = (
                f"[call {call_idx}] phase={candidate['phase']} "
                f"index={candidate['index']}/{candidate['total']}"
            )
            if candidate["phase"] == "boxes" and candidate["candidate"].get("valid"):
                c = candidate["candidate"]
                print(
                    f"{label} id={c.get('id')} var={c.get('varname')} "
                    f"class={c.get('maxclass')} text={repr(c.get('text'))}"
                )
            elif candidate["phase"] == "lines" and candidate["candidate"].get("valid"):
                c = candidate["candidate"]
                print(f"{label} src={c.get('source')} dst={c.get('destination')}")
            else:
                print(f"{label} candidate=<none>")

            payload: dict[str, Any] = {
                "action": "apply_topology_snapshot_progressive",
                "snapshot": snapshot,
                "chunk_size": args.chunk_size,
            }
            if state is not None:
                payload["progress_state"] = state
            if args.debug_timing:
                payload["debug_timing"] = True

            t0 = time.monotonic()
            try:
                response = await conn.send_request(payload, timeout=args.timeout_seconds)
            except Exception as exc:  # noqa: BLE001
                elapsed = round(time.monotonic() - t0, 3)
                error = _exc_to_dict(exc)
                print(f"[call {call_idx}] ERROR after {elapsed}s: {error.get('code', error['type'])} {error['message']}")
                chunk_event = {
                    "call": call_idx,
                    "elapsed_seconds": elapsed,
                    "candidate": candidate,
                    "error": error,
                }
                chunks.append(chunk_event)
                result["first_error"] = chunk_event
                return result

            elapsed = round(time.monotonic() - t0, 3)
            done = bool(response.get("done")) if isinstance(response, dict) else False
            progress = response.get("progress", {}) if isinstance(response, dict) else {}
            timing = response.get("timing", {}) if isinstance(response, dict) else {}
            chunk_event = {
                "call": call_idx,
                "elapsed_seconds": elapsed,
                "candidate": candidate,
                "done": done,
                "progress": progress,
                "timing": timing,
                "cursor": response.get("cursor", {}) if isinstance(response, dict) else {},
                "operations_this_chunk": (
                    response.get("operations_this_chunk") if isinstance(response, dict) else None
                ),
            }
            chunks.append(chunk_event)
            print(
                f"[call {call_idx}] OK elapsed={elapsed}s done={done} "
                f"processed={progress.get('processed')} remaining={progress.get('remaining')} "
                f"chunk_ms={timing.get('chunk_elapsed_ms')}"
            )

            if done:
                result["ok"] = True
                result["final_response"] = response
                return result

            state = response.get("state") if isinstance(response, dict) else None
            if not isinstance(state, dict):
                result["first_error"] = {
                    "call": call_idx,
                    "elapsed_seconds": elapsed,
                    "candidate": candidate,
                    "error": {
                        "type": "StateError",
                        "message": "Progressive response did not include continuation state.",
                    },
                }
                return result

        result["first_error"] = {
            "call": max_calls,
            "error": {
                "type": "IterationLimit",
                "message": f"Reached max_calls={max_calls} before completion.",
            },
        }
        return result
    finally:
        try:
            await conn.send_request({"action": "set_workspace_target", "target_id": "host"}, timeout=8.0)
        except Exception:
            pass
        if created_workspace and not args.keep_workspace:
            try:
                await conn.send_request(
                    {"action": "remove_object", "varname": workspace_varname},
                    timeout=8.0,
                )
            except Exception:
                pass
        await conn.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reproduce bridge progressive import timeout and identify the hanging candidate.",
    )
    parser.add_argument("--patch", required=True, help="Path to .maxpat patch file.")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1,
        help="Operations per progressive call (default: 1).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=20.0,
        help="Per-call timeout in seconds (default: 20).",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=0,
        help="Maximum progressive calls (0 = auto).",
    )
    parser.add_argument(
        "--workspace-varname",
        default="",
        help="Scratch workspace varname (default: auto-generated).",
    )
    parser.add_argument(
        "--debug-timing",
        action="store_true",
        help="Request bridge-side timing posts/fields per chunk.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep generated scratch workspace object after run.",
    )
    parser.add_argument(
        "--summary-json",
        default="",
        help="Optional path to write JSON summary.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "fatal_error": _exc_to_dict(exc)}, indent=2, sort_keys=True))
        return 1

    summary_json = json.dumps(result, indent=2, sort_keys=True)
    print("\n[summary]")
    print(summary_json)

    if args.summary_json:
        out_path = Path(args.summary_json).expanduser().resolve()
        out_path.write_text(summary_json + "\n")
        print(f"\nWrote summary to: {out_path}")

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
