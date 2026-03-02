import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server as server_module
from server import (
    ERROR_BRIDGE_TIMEOUT,
    ERROR_INTERNAL,
    ERROR_OVERLOADED,
    ERROR_PRECONDITION,
    ERROR_PROTO_V3_MISSING_FIELD,
    ERROR_UNKNOWN_ACTION,
    ERROR_UNAUTHORIZED,
    ERROR_VALIDATION,
    TRANSPORT_DICT_REF,
    MaxMCPError,
    MaxMSPConnection,
    MaxHygieneManager,
    MaxRuntimeManager,
    _normalize_add_object_spec,
    _normalize_avoid_rect_payload,
    _build_transaction_bridge_request,
    _ServerInstanceLock,
    _resolve_auth_token_from_sources,
    add_max_object,
    get_avoid_rect_position,
    get_bridge_slo_report,
    get_object_schema,
    maxpy_catalog,
    search_objects,
    qa_audit_patch,
    diff_patch_summary,
    validate_publish_readiness,
    dry_run_plan,
    run_patch_transaction,
)


class FakeSocketClient:
    def __init__(self, handler=None):
        self.connected = True
        self._handler = handler
        self.emits = []

    async def emit(self, event, data, namespace=None):
        self.emits.append((event, data, namespace))
        if self._handler:
            await self._handler(event, data, namespace)

    async def connect(self, *_args, **_kwargs):
        self.connected = True

    async def disconnect(self):
        self.connected = False


TEST_PROJECT_ID = "testproj"
TEST_WORKSPACE_ID = "main"
TEST_SCOPE = f"{TEST_PROJECT_ID}:{TEST_WORKSPACE_ID}"


class _ScopedRuntimeStub:
    async def activate_workspace(
        self,
        *,
        project_id: str,
        workspace_id: str,
        create_if_missing: bool = True,
    ) -> dict:
        _ = create_if_missing
        return {
            "success": True,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "target_id": f"{project_id}:{workspace_id}",
        }


def _make_scoped_ctx(maxmsp, runtime=None):
    selected_runtime = runtime if runtime is not None else _ScopedRuntimeStub()
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"maxmsp": maxmsp, "runtime": selected_runtime}
        )
    )


def _seed_workspace(runtime: MaxRuntimeManager) -> None:
    runtime.register_project(
        project_id=TEST_PROJECT_ID,
        create_default_workspace=False,
    )
    runtime.create_workspace(
        project_id=TEST_PROJECT_ID,
        workspace_id=TEST_WORKSPACE_ID,
    )
    runtime.active_project_id = TEST_PROJECT_ID
    runtime.active_workspace_id = TEST_WORKSPACE_ID
    runtime.active_target = TEST_SCOPE


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_request_wraps_v2_envelope(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "succeeded",
                    "results": {"ok": True},
                }
            )

        fake_sio = FakeSocketClient(handler=handler)
        conn.sio = fake_sio
        result = await conn.send_request({"action": "health_ping"}, timeout=1.0)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(fake_sio.emits), 1)
        event, payload, namespace = fake_sio.emits[0]
        self.assertEqual(event, "request")
        self.assertEqual(namespace, "/mcp")
        self.assertEqual(payload["protocol_version"], "2.0")
        self.assertEqual(payload["state"], "requested")
        self.assertEqual(payload["action"], "health_ping")
        self.assertEqual(payload["payload"], {})

    def test_build_request_envelope_does_not_mirror_payload_fields(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        envelope = conn._build_request_envelope(  # noqa: SLF001
            {
                "action": "add_object",
                "payload": {
                    "obj_type": "button",
                    "position": [10, 20],
                    "varname": "x1",
                },
            }
        )
        self.assertEqual(envelope["action"], "add_object")
        self.assertIn("payload", envelope)
        self.assertEqual(envelope["payload"]["obj_type"], "button")
        self.assertNotIn("obj_type", envelope)
        self.assertNotIn("position", envelope)
        self.assertNotIn("varname", envelope)

    async def test_refresh_capabilities_selects_dict_transport(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "succeeded",
                    "results": {
                        "bridge_proto": server_module.BRIDGE_PROTO,
                        "supported_actions": [],
                        "supported_transports": [TRANSPORT_DICT_REF],
                    },
                }
            )

        conn.sio = FakeSocketClient(handler=handler)
        caps = await conn.refresh_capabilities()
        self.assertEqual(caps.get("bridge_proto"), server_module.BRIDGE_PROTO)
        self.assertEqual(conn.request_transport, TRANSPORT_DICT_REF)
        self.assertIn(TRANSPORT_DICT_REF, conn.supported_transports)

    async def test_send_request_envelope_forces_dict_transport(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.request_transport = TRANSPORT_DICT_REF
        conn.supported_transports = [TRANSPORT_DICT_REF]

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "succeeded",
                    "results": {"transport": payload.get("transport")},
                }
            )

        fake_sio = FakeSocketClient(handler=handler)
        conn.sio = fake_sio
        result = await conn.send_request(
            {"action": "health_ping", "transport": "framed_json"},
            timeout=1.0,
        )
        self.assertEqual(result.get("transport"), TRANSPORT_DICT_REF)
        self.assertEqual(fake_sio.emits[0][1].get("transport"), TRANSPORT_DICT_REF)

    async def test_send_request_rejects_capabilities_without_dict_transport(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        fake_sio = FakeSocketClient(handler=None)
        conn.sio = fake_sio
        conn.capabilities = {
            "supported_actions": ["health_ping"],
            "supported_transports": ["framed_json"],
        }

        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request({"action": "health_ping"}, timeout=1.0)
        self.assertEqual(ctx.exception.code, ERROR_PRECONDITION)
        self.assertEqual(len(fake_sio.emits), 0)

    async def test_refresh_capabilities_without_dict_transport_blocks_requests(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "succeeded",
                    "results": {
                        "bridge_proto": server_module.BRIDGE_PROTO,
                        "supported_actions": ["health_ping"],
                        "supported_transports": ["framed_json"],
                    },
                }
            )

        fake_sio = FakeSocketClient(handler=handler)
        conn.sio = fake_sio
        caps = await conn.refresh_capabilities()
        self.assertEqual(caps.get("supported_transports"), ["framed_json"])
        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request({"action": "health_ping"}, timeout=1.0)
        self.assertEqual(ctx.exception.code, ERROR_PRECONDITION)
        self.assertEqual(len(fake_sio.emits), 1)

    async def test_send_request_rejects_legacy_response_shape_when_strict(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                conn._normalize_response({"request_id": req_id, "results": {"legacy": True}})
            )

        conn.sio = FakeSocketClient(handler=handler)
        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request({"action": "health_ping"}, timeout=1.0)
        self.assertEqual(ctx.exception.code, ERROR_PROTO_V3_MISSING_FIELD)

    async def test_send_request_rejects_legacy_response_when_legacy_flag_disabled(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.strict_v2_enforcement = False

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                conn._normalize_response({"request_id": req_id, "results": {"legacy": True}})
            )

        conn.sio = FakeSocketClient(handler=handler)
        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request({"action": "health_ping"}, timeout=1.0)
        self.assertEqual(ctx.exception.code, ERROR_PROTO_V3_MISSING_FIELD)

    async def test_send_request_raises_structured_failure(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "failed",
                    "error": {
                        "code": ERROR_INTERNAL,
                        "message": "Bridge exploded",
                        "recoverable": False,
                        "details": {"where": "unit-test"},
                    },
                }
            )

        conn.sio = FakeSocketClient(handler=handler)
        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request({"action": "health_ping"}, timeout=1.0)
        self.assertEqual(ctx.exception.code, ERROR_INTERNAL)
        self.assertEqual(ctx.exception.details["where"], "unit-test")

    async def test_transport_failure_streak_clears_capabilities(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.capabilities = {
            "supported_actions": ["health_ping", "workspace_status"],
            "supported_transports": [TRANSPORT_DICT_REF],
        }

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "failed",
                    "error": {
                        "code": "TRANSPORT_PROTOCOL_ERROR",
                        "message": "Failed to hand off request through dictionary transport.",
                        "recoverable": True,
                        "details": {
                            "required_transport": TRANSPORT_DICT_REF,
                            "event": "request",
                        },
                    },
                }
            )

        conn.sio = FakeSocketClient(handler=handler)
        with self.assertRaises(MaxMCPError):
            await conn.send_request({"action": "health_ping"}, timeout=1.0)
        self.assertGreaterEqual(conn.transport_failure_streak, 1)
        with self.assertRaises(MaxMCPError):
            await conn.send_request({"action": "health_ping"}, timeout=1.0)
        self.assertEqual(conn.capabilities, {})
        self.assertEqual(conn.request_transport, TRANSPORT_DICT_REF)

    async def test_send_request_timeout_maps_to_bridge_timeout_error(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.sio = FakeSocketClient(handler=None)
        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request({"action": "health_ping"}, timeout=0.01)
        self.assertEqual(ctx.exception.code, ERROR_BRIDGE_TIMEOUT)

    async def test_send_request_timeout_budget_exhausted_by_queue_wait(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.sio = FakeSocketClient(handler=None)
        conn.capabilities = {"supported_actions": ["add_object"]}

        async def fake_acquire_mutation_slot(_action: str) -> float:
            await asyncio.sleep(0.2)
            return 0.2

        conn._acquire_mutation_slot = fake_acquire_mutation_slot  # type: ignore[method-assign]

        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request(
                {
                    "action": "add_object",
                    "position": [10, 10],
                    "obj_type": "button",
                    "varname": "queue_timeout_obj",
                    "args": [],
                },
                timeout=0.05,
            )
        self.assertEqual(ctx.exception.code, ERROR_BRIDGE_TIMEOUT)
        self.assertEqual(len(conn.sio.emits), 0)
        self.assertGreaterEqual(conn.total_timeouts, 1)

    async def test_idempotency_cache_prevents_duplicate_emit(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "succeeded",
                    "results": {"sequence": len(fake_sio.emits)},
                }
            )

        fake_sio = FakeSocketClient(handler=handler)
        conn.sio = fake_sio

        first = await conn.send_request(
            {"action": "health_ping"},
            timeout=1.0,
            idempotency_key="abc-123",
        )
        second = await conn.send_request(
            {"action": "health_ping"},
            timeout=1.0,
            idempotency_key="abc-123",
        )

        self.assertEqual(first, second)
        self.assertEqual(len(fake_sio.emits), 1)

    async def test_capability_gating_blocks_unsupported_action(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.capabilities = {"supported_actions": ["health_ping"]}
        conn.sio = FakeSocketClient(handler=None)
        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request({"action": "add_object", "position": [0, 0]}, timeout=0.1)
        self.assertEqual(ctx.exception.code, ERROR_PRECONDITION)

    async def test_auth_token_is_attached_to_request_envelope(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.auth_token = "unit-test-token"

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "succeeded",
                    "results": {"auth_token_seen": payload.get("auth_token")},
                }
            )

        conn.sio = FakeSocketClient(handler=handler)
        result = await conn.send_request({"action": "health_ping"}, timeout=1.0)
        self.assertEqual(result["auth_token_seen"], "unit-test-token")

    async def test_mutation_queue_rejects_when_full(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.sio = FakeSocketClient(handler=None)
        conn.capabilities = {"supported_actions": ["add_object"]}
        conn._mutation_waiters.clear()
        for i in range(conn.mutation_max_queue):
            conn._mutation_waiters.append(f"queued-{i}")
        conn._queued_mutation_requests = len(conn._mutation_waiters)
        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request(
                {"action": "add_object", "position": [0, 0], "obj_type": "button", "varname": "a"},
                timeout=0.1,
            )
        self.assertEqual(ctx.exception.code, ERROR_OVERLOADED)

    async def test_metrics_snapshot_tracks_action_counts(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "succeeded",
                    "results": {"ok": True},
                }
            )

        conn.sio = FakeSocketClient(handler=handler)
        await conn.send_request({"action": "health_ping"}, timeout=1.0)
        metrics = conn.metrics_snapshot()
        self.assertEqual(metrics["actions"]["health_ping"]["requests"], 1)
        self.assertEqual(metrics["actions"]["health_ping"]["failed"], 0)
        self.assertGreaterEqual(metrics["latency_samples"], 1)

    async def test_mutation_queue_preserves_fifo_order(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.mutation_max_inflight = 1
        conn.mutation_max_queue = 8
        conn._mutation_waiters.clear()
        conn._queued_mutation_requests = 0
        conn._inflight_mutation_requests = 0
        conn.capabilities = {"supported_actions": ["add_object"]}
        seen = []

        async def handler(_event, payload, _namespace):
            embedded = payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
            varname = embedded.get("varname")
            seen.append(varname)
            await asyncio.sleep(0.02)
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "succeeded",
                    "results": {"ok": True, "varname": varname},
                }
            )

        conn.sio = FakeSocketClient(handler=handler)

        async def send_add(varname: str):
            return await conn.send_request(
                {
                    "action": "add_object",
                    "position": [10, 10],
                    "obj_type": "button",
                    "varname": varname,
                    "args": [],
                },
                timeout=2.0,
            )

        t1 = asyncio.create_task(send_add("fifo_1"))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(send_add("fifo_2"))
        await asyncio.sleep(0)
        t3 = asyncio.create_task(send_add("fifo_3"))
        results = await asyncio.gather(t1, t2, t3)
        self.assertEqual(seen, ["fifo_1", "fifo_2", "fifo_3"])
        self.assertEqual([r["varname"] for r in results], ["fifo_1", "fifo_2", "fifo_3"])

    async def test_start_server_sends_auth_token_in_handshake(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.auth_token = "handshake-token"

        class DummySio:
            def __init__(self):
                self.connected = False
                self.url = None
                self.kwargs = None

            async def connect(self, url, **kwargs):
                self.connected = True
                self.url = url
                self.kwargs = kwargs

        dummy = DummySio()
        conn.sio = dummy

        async def fake_refresh():
            return {"supported_actions": []}

        conn.refresh_capabilities = fake_refresh
        ok = await conn.start_server()
        self.assertTrue(ok)
        self.assertEqual(dummy.kwargs["namespaces"], ["/mcp"])
        self.assertEqual(dummy.kwargs["auth"]["token"], "handshake-token")
        self.assertEqual(dummy.kwargs["headers"]["x-maxmcp-token"], "handshake-token")

    async def test_ensure_connected_rejects_missing_token_when_auth_required(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.auth_token = ""
        conn.auth_token_source = "none"
        conn.require_handshake_auth = True
        conn.sio = FakeSocketClient(handler=None)
        conn.sio.connected = False
        with self.assertRaises(MaxMCPError) as ctx:
            await conn.ensure_connected(retries=0)
        self.assertEqual(ctx.exception.code, ERROR_PRECONDITION)

    def test_resolve_auth_token_prefers_env_then_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "auth_token"
            token_file.write_text("file-token", encoding="utf-8")
            token, source = _resolve_auth_token_from_sources("env-token", token_file)
            self.assertEqual(token, "env-token")
            self.assertEqual(source, "env")

            token, source = _resolve_auth_token_from_sources("", token_file)
            self.assertEqual(token, "file-token")
            self.assertEqual(source, "file")

    def test_metrics_snapshot_alerts_trigger_and_clear(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        now = time.time()

        healthy_samples = []
        for _ in range(20):
            healthy_samples.append(
                {
                    "duration_ms": 20.0,
                    "queue_wait_ms": 1.0,
                    "timestamp": now,
                    "action": "health_ping",
                    "state": "succeeded",
                }
            )
        conn._latency_samples.extend(healthy_samples)
        conn._queued_mutation_requests = 0
        healthy_metrics = conn.metrics_snapshot()
        self.assertEqual(healthy_metrics.get("alerts"), [])

        conn._latency_samples.clear()
        unhealthy_samples = []
        for i in range(20):
            unhealthy_samples.append(
                {
                    "duration_ms": 5000.0 if i > 10 else 2500.0,
                    "queue_wait_ms": 200.0,
                    "timestamp": now,
                    "action": "add_object",
                    "state": "failed" if i % 2 == 0 else "timeout",
                    "code": ERROR_BRIDGE_TIMEOUT,
                }
            )
        conn._latency_samples.extend(unhealthy_samples)
        conn._queued_mutation_requests = conn.mutation_max_queue
        unhealthy_metrics = conn.metrics_snapshot()
        alert_codes = {a.get("code") for a in unhealthy_metrics.get("alerts", [])}
        self.assertIn("ALERT_FAILURE_RATE", alert_codes)
        self.assertIn("ALERT_P95_LATENCY", alert_codes)
        self.assertIn("ALERT_QUEUE_SATURATION", alert_codes)

    def test_emit_metrics_log_sets_timestamp(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        payload = conn.emit_metrics_log(force=True)
        self.assertIsInstance(payload, dict)
        self.assertIsNotNone(conn.last_metrics_log_emit_at)
        self.assertIn("metrics", payload)

    def test_metrics_snapshot_alerts_on_sustained_file_fallback_ratio(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.transport_health = {
            "handoff_stats": {
                "dict_attempts": 30,
                "dict_successes": 20,
                "dict_failures": 10,
                "file_fallback_attempts": 12,
                "file_fallback_successes": 10,
                "file_fallback_failures": 2,
                "last_handoff_mode": "file_ref",
            }
        }
        metrics = conn.metrics_snapshot()
        alerts = {item.get("code") for item in metrics.get("alerts", [])}
        self.assertIn("ALERT_TRANSPORT_FILE_FALLBACK", alerts)
        rolling = metrics.get("rolling_windows", {})
        self.assertEqual(rolling.get("transport_total_handoff_successes"), 30)
        self.assertAlmostEqual(
            float(rolling.get("transport_file_fallback_ratio", 0.0)),
            10.0 / 30.0,
            places=6,
        )

    def test_metrics_snapshot_no_file_fallback_alert_before_min_successes(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.transport_health = {
            "handoff_stats": {
                "dict_successes": 2,
                "file_fallback_successes": 3,
            }
        }
        metrics = conn.metrics_snapshot()
        alerts = {item.get("code") for item in metrics.get("alerts", [])}
        self.assertNotIn("ALERT_TRANSPORT_FILE_FALLBACK", alerts)
        self.assertEqual(metrics["transport_handoff"]["total_successes"], 5)


class ServerLockTests(unittest.TestCase):
    def test_server_instance_lock_writes_metadata_and_releases_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "server.lock"
            lock = _ServerInstanceLock(lock_path)
            lock.acquire()
            try:
                self.assertTrue(lock_path.exists())
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload.get("pid"), os.getpid())
                self.assertEqual(payload.get("lock_path"), str(lock_path))
                self.assertIn("hostname", payload)
                self.assertIn("acquired_at_epoch", payload)
            finally:
                lock.release()
            self.assertFalse(lock_path.exists())

    def test_server_instance_lock_reports_live_holder_on_lock_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "server.lock"
            lock_path.write_text(json.dumps({"pid": 43210}), encoding="utf-8")
            lock = _ServerInstanceLock(lock_path)

            def _raise_blocking(_fd, _flags):
                raise BlockingIOError("synthetic lock conflict")

            fake_fcntl = SimpleNamespace(
                LOCK_EX=1,
                LOCK_NB=2,
                LOCK_UN=8,
                flock=_raise_blocking,
            )

            with patch.object(server_module, "fcntl", fake_fcntl):
                with patch.object(_ServerInstanceLock, "_pid_alive", return_value=True):
                    with self.assertRaises(RuntimeError) as ctx:
                        lock.acquire()

            self.assertIn("already running", str(ctx.exception))


class BridgeSLOTests(unittest.TestCase):
    def test_get_bridge_slo_report_with_series(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        now = time.time()
        conn._latency_samples.clear()
        conn._latency_samples.extend(
            [
                {
                    "duration_ms": 40.0,
                    "queue_wait_ms": 2.0,
                    "timestamp": now - 40.0,
                    "action": "health_ping",
                    "state": "succeeded",
                },
                {
                    "duration_ms": 75.0,
                    "queue_wait_ms": 4.0,
                    "timestamp": now - 20.0,
                    "action": "add_object",
                    "state": "failed",
                    "code": ERROR_INTERNAL,
                },
                {
                    "duration_ms": 65.0,
                    "queue_wait_ms": 3.0,
                    "timestamp": now - 5.0,
                    "action": "add_object",
                    "state": "timeout",
                    "code": ERROR_BRIDGE_TIMEOUT,
                },
            ]
        )
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context={"maxmsp": conn})
        )
        report = get_bridge_slo_report(
            ctx,
            window_seconds=120.0,
            include_series=True,
            max_points=12,
        )
        self.assertEqual(report["request_count"], 3)
        self.assertEqual(report["failure_count"], 2)
        self.assertGreater(report["rates"]["failure_rate"], 0.0)
        self.assertIn("series", report)
        self.assertGreaterEqual(len(report["series"]), 1)
        self.assertIn(report["status"], {"pass", "warn", "fail"})

    def test_get_bridge_slo_report_without_bridge(self):
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context={}))
        report = get_bridge_slo_report(ctx)
        self.assertFalse(report["success"])
        self.assertEqual(report["error"]["code"], "BRIDGE_UNAVAILABLE")


class DryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_validates_and_tracks_virtual_depth(self):
        fake_conn = SimpleNamespace()

        async def fake_send_request(_payload, timeout=2.0):
            return {"depth": 0, "path": [], "is_root": True}

        fake_conn.send_request = fake_send_request
        ctx = _make_scoped_ctx(fake_conn)
        plan = [
            {"action": "enter_subpatcher", "params": {"varname": "p1"}},
            {"action": "add_max_object", "params": {"position": [0, 0], "obj_type": "+", "varname": "n1", "args": [0]}},
            {"action": "exit_subpatcher", "params": {}},
        ]
        result = await dry_run_plan(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            plan,
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["ending_virtual_depth"], 0)
        self.assertGreaterEqual(len(result["errors"]), 1)

    async def test_dry_run_maxpy_detects_invalid_connection_indices(self):
        fake_conn = SimpleNamespace()

        async def fake_send_request(payload, timeout=2.0):
            action = payload.get("action")
            if action == "get_patcher_context":
                return {"depth": 0, "path": [], "is_root": True}
            if action == "get_objects_in_patch":
                return {
                    "boxes": [
                        {"box": {"varname": "osc1", "maxclass": "newobj", "numinlets": 2, "numoutlets": 1}},
                        {"box": {"varname": "gain1", "maxclass": "newobj", "numinlets": 2, "numoutlets": 1}},
                    ],
                    "lines": [],
                }
            return {}

        fake_conn.send_request = fake_send_request
        ctx = _make_scoped_ctx(fake_conn)

        plan = [
            {
                "action": "connect_max_objects",
                "params": {
                    "src_varname": "osc1",
                    "outlet_idx": 9,
                    "dst_varname": "gain1",
                    "inlet_idx": 0,
                },
            }
        ]
        result = await dry_run_plan(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            plan,
            engine="maxpy",
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["engine"], "maxpy")
        self.assertGreaterEqual(len(result["errors"]), 1)

    async def test_dry_run_unknown_action_defaults_to_error(self):
        fake_conn = SimpleNamespace()

        async def fake_send_request(_payload, timeout=2.0):
            return {"depth": 0, "path": [], "is_root": True}

        fake_conn.send_request = fake_send_request
        ctx = _make_scoped_ctx(fake_conn)
        result = await dry_run_plan(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            steps=[{"action": "totally_new_action", "params": {}}],
        )
        self.assertFalse(result["valid"])
        self.assertTrue(any(err.get("code") == ERROR_UNKNOWN_ACTION for err in result["errors"]))

    async def test_dry_run_unknown_action_warn_policy(self):
        fake_conn = SimpleNamespace()

        async def fake_send_request(_payload, timeout=2.0):
            return {"depth": 0, "path": [], "is_root": True}

        fake_conn.send_request = fake_send_request
        ctx = _make_scoped_ctx(fake_conn)
        result = await dry_run_plan(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            steps=[{"action": "totally_new_action", "params": {}}],
            unknown_action_policy="warn",
        )
        self.assertTrue(result["valid"])
        self.assertTrue(any(warn.get("code") == ERROR_UNKNOWN_ACTION for warn in result["warnings"]))

    async def test_dry_run_accepts_legacy_string_steps_with_warning(self):
        fake_conn = SimpleNamespace()

        async def fake_send_request(_payload, timeout=2.0):
            return {"depth": 0, "path": [], "is_root": True}

        fake_conn.send_request = fake_send_request
        ctx = _make_scoped_ctx(fake_conn)
        result = await dry_run_plan(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            steps=["check_signal_safety", "exit_subpatcher"],
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["ending_virtual_depth"], 0)
        self.assertTrue(
            any("Legacy string step format is deprecated" in warn.get("message", "") for warn in result["warnings"])
        )


class ToolCompatTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_add_object_spec_newobj_rewrite(self):
        obj_type, args, rewrite, err = _normalize_add_object_spec("newobj", ["prepend", "set"])
        self.assertIsNone(err)
        self.assertEqual(obj_type, "prepend")
        self.assertEqual(args, ["set"])
        self.assertTrue(rewrite["applied"])

    def test_normalize_add_object_spec_newobj_invalid_without_args(self):
        _obj_type, _args, _rewrite, err = _normalize_add_object_spec("newobj", [])
        self.assertIsNotNone(err)
        self.assertFalse(err["success"])
        self.assertEqual(err["error"]["code"], "VALIDATION_ERROR")

    def test_normalize_avoid_rect_payload_falls_back_for_invalid(self):
        rect, valid = _normalize_avoid_rect_payload({"bad": "shape"})
        self.assertEqual(rect, [0.0, 0.0, 0.0, 0.0])
        self.assertFalse(valid)

    async def test_add_max_object_auto_preflight_and_newobj_shim(self):
        actions = []

        class FakeBridge:
            def __init__(self):
                self.preflight_auto_calls = 0
                self.preflight_cache_hits = 0
                self.preflight_invalid_rects = 0
                self.newobj_compat_rewrites = 0
                self._preflight_last_at = 0.0

            async def send_request(self, payload, timeout=2.0, idempotency_key=None):
                actions.append((payload.get("action"), json.loads(json.dumps(payload))))
                if payload.get("action") == "get_avoid_rect_position":
                    return [0, 0, 0, 0]
                if payload.get("action") == "add_object":
                    return {"success": True}
                return {"success": True}

        bridge = FakeBridge()
        ctx = _make_scoped_ctx(bridge)
        original_mode = server_module.MAXMCP_PREFLIGHT_MODE
        try:
            server_module.MAXMCP_PREFLIGHT_MODE = "auto"
            result = await add_max_object(
                ctx,
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                position=[10, 20],
                obj_type="newobj",
                varname="compat_obj",
                args=["prepend", "set_folderPath"],
            )
        finally:
            server_module.MAXMCP_PREFLIGHT_MODE = original_mode

        self.assertTrue(result["success"])
        self.assertEqual(actions[0][0], "get_avoid_rect_position")
        self.assertEqual(actions[1][0], "add_object")
        self.assertEqual(actions[1][1]["obj_type"], "prepend")
        self.assertEqual(actions[1][1]["args"], ["set_folderPath"])
        self.assertIn("meta", result)
        self.assertEqual(result["meta"]["newobj_compat"]["resolved_obj_type"], "prepend")
        self.assertTrue(result["meta"]["preflight"]["performed"])

    async def test_get_avoid_rect_position_sanitizes_bridge_payload(self):
        class FakeBridge:
            def __init__(self):
                self._preflight_last_at = 0.0
                self.preflight_invalid_rects = 0

            async def send_request(self, payload, timeout=2.0, idempotency_key=None):
                if payload.get("action") == "get_avoid_rect_position":
                    return [None, "x", {}, []]
                return {}

        bridge = FakeBridge()
        ctx = _make_scoped_ctx(bridge)
        rect = await get_avoid_rect_position(
            ctx,
            project_id=TEST_PROJECT_ID,
            workspace_id=TEST_WORKSPACE_ID,
        )
        self.assertEqual(rect, [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(bridge.preflight_invalid_rects, 1)

    async def test_add_max_object_prefers_atomic_preflight_when_supported(self):
        actions = []

        class FakeBridge:
            def __init__(self):
                self.capabilities = {
                    "supported_actions": ["add_object_with_preflight"]
                }
                self.preflight_auto_calls = 0
                self.preflight_cache_hits = 0
                self.preflight_invalid_rects = 0
                self.newobj_compat_rewrites = 0
                self._preflight_last_at = 0.0

            async def send_request(self, payload, timeout=2.0, idempotency_key=None):
                actions.append((payload.get("action"), json.loads(json.dumps(payload))))
                return {"success": True}

        bridge = FakeBridge()
        ctx = _make_scoped_ctx(bridge)
        original_mode = server_module.MAXMCP_PREFLIGHT_MODE
        try:
            server_module.MAXMCP_PREFLIGHT_MODE = "auto"
            result = await add_max_object(
                ctx,
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                position=[10, 20],
                obj_type="button",
                varname="atomic_obj",
                args=[],
            )
        finally:
            server_module.MAXMCP_PREFLIGHT_MODE = original_mode

        self.assertTrue(result["success"])
        self.assertEqual([entry[0] for entry in actions], ["add_object_with_preflight"])
        self.assertTrue(result["meta"]["preflight"]["atomic"])
        self.assertEqual(result["meta"]["preflight"]["reason"], "atomic_bridge_preflight")


class PublishReadinessToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_qa_audit_patch_reports_findings(self):
        class FakeBridge:
            async def send_request(self, payload, timeout=2.0, idempotency_key=None):
                _ = timeout
                _ = idempotency_key
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    return {
                        "boxes": [
                            {
                                "box": {
                                    "maxclass": "newobj",
                                    "varname": "p1",
                                    "boxtext": "print debug_value",
                                    "patching_rect": [10.5, 20, 80, 20],
                                }
                            },
                            {
                                "box": {
                                    "maxclass": "newobj",
                                    "varname": "audio_out",
                                    "boxtext": "dac~",
                                    "patching_rect": [120, 20, 60, 20],
                                }
                            },
                            {
                                "box": {
                                    "maxclass": "comment",
                                    "varname": "c1",
                                    "text": "TODO remove debug",
                                    "patching_rect": [10, 60, 120, 20],
                                }
                            },
                        ],
                        "lines": [
                            {
                                "patchline": {
                                    "source": ["p1", 0],
                                    "destination": ["audio_out", 0],
                                    "midpoints": [40, 20, 100, 20],
                                }
                            }
                        ],
                    }
                if action == "check_signal_safety":
                    return {
                        "safe": False,
                        "warnings": [
                            {
                                "type": "FEEDBACK_LOOP",
                                "message": "Potentially dangerous feedback loop detected",
                                "objects": ["p1", "audio_out"],
                            }
                        ],
                    }
                return {}

        ctx = _make_scoped_ctx(FakeBridge())
        result = await qa_audit_patch(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
        )
        self.assertTrue(result["success"])
        self.assertGreater(result["summary"]["critical_findings"], 0)
        self.assertTrue(
            any(finding.get("id") == "no_print_objects" for finding in result["findings"])
        )
        self.assertLess(result["score"]["overall"], 100.0)

    async def test_validate_publish_readiness_passes_clean_patch(self):
        class FakeBridge:
            async def send_request(self, payload, timeout=2.0, idempotency_key=None):
                _ = timeout
                _ = idempotency_key
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    return {
                        "boxes": [
                            {
                                "box": {
                                    "maxclass": "newobj",
                                    "varname": "osc1",
                                    "boxtext": "cycle~ 220",
                                    "patching_rect": [10, 20, 80, 20],
                                }
                            }
                        ],
                        "lines": [],
                    }
                if action == "check_signal_safety":
                    return {"safe": True, "warnings": []}
                return {}

        ctx = _make_scoped_ctx(FakeBridge())
        result = await validate_publish_readiness(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            min_score=70.0,
            require_extended_checks=False,
            write_report=False,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["report"]["gate_failures"], [])

    async def test_validate_publish_readiness_fails_on_critical_findings(self):
        class FakeBridge:
            async def send_request(self, payload, timeout=2.0, idempotency_key=None):
                _ = timeout
                _ = idempotency_key
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    return {
                        "boxes": [
                            {
                                "box": {
                                    "maxclass": "newobj",
                                    "varname": "p1",
                                    "boxtext": "print debug",
                                    "patching_rect": [10, 20, 80, 20],
                                }
                            }
                        ],
                        "lines": [],
                    }
                if action == "check_signal_safety":
                    return {
                        "safe": False,
                        "warnings": [
                            {
                                "type": "UNSAFE_FEEDBACK",
                                "message": "comb~ feedback >= 1.0 will cause runaway gain",
                            }
                        ],
                    }
                return {}

        ctx = _make_scoped_ctx(FakeBridge())
        result = await validate_publish_readiness(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            min_score=95.0,
            require_extended_checks=False,
            write_report=False,
        )
        self.assertTrue(result["success"])
        self.assertFalse(result["ready"])
        self.assertGreater(len(result["report"]["gate_failures"]), 0)

    async def test_validate_publish_readiness_fails_when_extended_check_fails(self):
        class FakeBridge:
            async def send_request(self, payload, timeout=2.0, idempotency_key=None):
                _ = timeout
                _ = idempotency_key
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    return {
                        "boxes": [
                            {
                                "box": {
                                    "maxclass": "newobj",
                                    "varname": "osc1",
                                    "boxtext": "cycle~ 220",
                                    "patching_rect": [10, 20, 80, 20],
                                }
                            }
                        ],
                        "lines": [],
                    }
                if action == "check_signal_safety":
                    return {"safe": True, "warnings": []}
                return {}

        ctx = _make_scoped_ctx(FakeBridge())
        with patch.object(
            server_module,
            "_run_maxpylang_check_extended_from_topology",
            return_value={
                "enabled": True,
                "available": True,
                "passed": False,
                "failures": ["synthetic failure"],
                "warnings": [],
                "metrics": {},
                "command": ["maxpylang"],
                "input_path": "/tmp/synthetic.maxpat",
            },
        ):
            result = await validate_publish_readiness(
                ctx,
                TEST_PROJECT_ID,
                TEST_WORKSPACE_ID,
                min_score=70.0,
                require_extended_checks=True,
                write_report=False,
            )
        self.assertTrue(result["success"])
        self.assertFalse(result["ready"])
        self.assertTrue(
            any(item.get("gate") == "extended_checks" for item in result["report"]["gate_failures"])
        )
        self.assertIn("extended_checks", result["report"])
        self.assertIn("protocol_v3", result["report"])

    async def test_validate_publish_readiness_chaos_gate_requires_report(self):
        class FakeBridge:
            async def send_request(self, payload, timeout=2.0, idempotency_key=None):
                _ = timeout
                _ = idempotency_key
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    return {
                        "boxes": [
                            {
                                "box": {
                                    "maxclass": "newobj",
                                    "varname": "osc1",
                                    "boxtext": "cycle~ 220",
                                    "patching_rect": [10, 20, 80, 20],
                                }
                            }
                        ],
                        "lines": [],
                    }
                if action == "check_signal_safety":
                    return {"safe": True, "warnings": []}
                return {}

        ctx = _make_scoped_ctx(FakeBridge())
        result = await validate_publish_readiness(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            min_score=70.0,
            require_extended_checks=False,
            require_chaos_gate=True,
            chaos_report_path="",
            write_report=False,
        )
        self.assertTrue(result["success"])
        self.assertFalse(result["ready"])
        self.assertTrue(any(item.get("gate") == "chaos_gate" for item in result["report"]["gate_failures"]))
        self.assertEqual(result["report"]["chaos_gate"]["required"], True)
        self.assertEqual(result["report"]["chaos_gate"]["executed"], False)

    async def test_validate_publish_readiness_accepts_valid_chaos_report(self):
        class FakeBridge:
            async def send_request(self, payload, timeout=2.0, idempotency_key=None):
                _ = timeout
                _ = idempotency_key
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    return {
                        "boxes": [
                            {
                                "box": {
                                    "maxclass": "newobj",
                                    "varname": "osc1",
                                    "boxtext": "cycle~ 220",
                                    "patching_rect": [10, 20, 80, 20],
                                }
                            }
                        ],
                        "lines": [],
                    }
                if action == "check_signal_safety":
                    return {"safe": True, "warnings": []}
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "chaos.json"
            report_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "summary": {"passed": True, "failures": []},
                        "aggregate_slo": {"passed": True},
                        "scenario_results": [],
                    }
                ),
                encoding="utf-8",
            )
            ctx = _make_scoped_ctx(FakeBridge())
            result = await validate_publish_readiness(
                ctx,
                TEST_PROJECT_ID,
                TEST_WORKSPACE_ID,
                min_score=70.0,
                require_extended_checks=False,
                require_chaos_gate=True,
                chaos_report_path=str(report_path),
                write_report=False,
            )
        self.assertTrue(result["success"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["report"]["chaos_gate"]["required"], True)
        self.assertEqual(result["report"]["chaos_gate"]["executed"], True)
        self.assertEqual(result["report"]["chaos_gate"]["passed"], True)

    def test_diff_patch_summary_internal_renderer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            before_path = tmp_path / "before.maxpat"
            after_path = tmp_path / "after.maxpat"
            before_path.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj", "varname": "a"}}],
                            "lines": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            after_path.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj", "varname": "b"}}],
                            "lines": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            diff = diff_patch_summary(
                None,
                before_path=str(before_path),
                after_path=str(after_path),
                prefer_maxdiff=False,
                max_output_lines=120,
            )
            self.assertTrue(diff["success"])
            self.assertGreater(diff["summary"]["changed_lines"], 0)
            self.assertEqual(diff["summary"]["before_backend"], "internal")
            self.assertIn("@@", diff["diff_preview"])


class MaxPyCatalogTests(unittest.TestCase):
    def test_maxpy_catalog_has_core_objects(self):
        self.assertTrue(maxpy_catalog.available)
        schema = maxpy_catalog.get_schema("trigger")
        self.assertIsNotNone(schema)
        self.assertEqual(schema["canonical_name"], "trigger")
        self.assertTrue(bool(maxpy_catalog.schema_hash))

    def test_maxpy_catalog_resolves_alias(self):
        canonical, via_alias = maxpy_catalog.resolve_name("t")
        self.assertTrue(via_alias)
        self.assertEqual(canonical, "trigger")

    def test_search_objects_includes_docs_fallback_for_live_path(self):
        result = search_objects(None, query="live.path")
        self.assertGreaterEqual(result["count"], 1)
        self.assertTrue(any(row.get("name") == "live.path" for row in result["results"]))
        self.assertTrue(any(row.get("source") in {"docs", "merged"} for row in result["results"]))

    def test_get_object_schema_falls_back_to_docs_when_schema_missing(self):
        result = get_object_schema(None, "live.path")
        if isinstance(result, dict) and result.get("success") is False:
            self.skipTest("live.path exists in MaxPy catalog for this environment.")
        self.assertFalse(result["schema_available"])
        self.assertTrue(result["doc_available"])
        self.assertEqual(result["source"], "docs_fallback")


class HygieneInventoryTests(unittest.TestCase):
    def test_parse_elapsed_seconds_supports_etime_format(self):
        self.assertEqual(MaxHygieneManager._parse_elapsed_seconds("59"), 59)
        self.assertEqual(MaxHygieneManager._parse_elapsed_seconds("01:05"), 65)
        self.assertEqual(MaxHygieneManager._parse_elapsed_seconds("02:03:04"), 7384)
        self.assertEqual(MaxHygieneManager._parse_elapsed_seconds("1-00:00:01"), 86401)
        self.assertIsNone(MaxHygieneManager._parse_elapsed_seconds("bad"))

    def test_inventory_exposes_scan_diagnostics(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.sio = SimpleNamespace(connected=True)
        runtime = MaxRuntimeManager(conn)
        hygiene = MaxHygieneManager(runtime, conn)
        hygiene._read_process_table_sync = lambda: (
            [],
            {
                "available": False,
                "method": "ps_axo",
                "fallback_used": True,
                "error": "ps_timeout",
                "attempts": [{"method": "ps_axo"}, {"method": "ps_Ac"}],
            },
        )
        hygiene._scan_open_documents_sync = lambda: {
            "available": False,
            "method": "osascript_jxa",
            "reason": "osascript_error:timed out",
            "failure_kind": "timeout",
            "timeout_seconds": 3.0,
            "documents": [],
        }
        hygiene._discover_managed_session_dirs_sync = lambda: []
        inventory = hygiene._build_inventory_sync(
            include_windows=True,
            include_runtime_state=False,
        )
        self.assertIn("process_scan", inventory)
        self.assertEqual(inventory["process_scan"]["method"], "ps_axo")
        self.assertIn("window_scan", inventory)
        self.assertEqual(inventory["window_scan"]["failure_kind"], "timeout")
        self.assertEqual(inventory["summary"]["process_inventory_confidence"], "low")


class RuntimeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_status_applies_backoff_after_transport_failure(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.sio = SimpleNamespace(connected=True)
        runtime = MaxRuntimeManager(conn)
        runtime.health_check_cooldown_seconds = 0.5
        runtime.failure_backoff_max_seconds = 1.0
        calls = {"health_ping": 0, "workspace_status": 0}

        async def fake_send_request(payload, timeout=2.0, **_kwargs):
            action = payload.get("action")
            if action == "health_ping":
                calls["health_ping"] += 1
                raise MaxMCPError(
                    "TRANSPORT_PROTOCOL_ERROR",
                    "Failed to hand off request through dictionary transport.",
                    recoverable=True,
                    details={"required_transport": TRANSPORT_DICT_REF},
                )
            if action == "workspace_status":
                calls["workspace_status"] += 1
                return {"success": True}
            return {"success": True}

        conn.send_request = fake_send_request  # type: ignore[method-assign]
        first = await runtime.collect_status(check_bridge=True)
        self.assertFalse(first["bridge_healthy"])
        self.assertIn("bridge_probe_backoff_seconds", first)
        self.assertEqual(calls["workspace_status"], 0)

        second = await runtime.collect_status(check_bridge=True)
        self.assertFalse(second["bridge_healthy"])
        self.assertTrue(second.get("bridge_probe_skipped"))
        self.assertEqual(calls["health_ping"], 1)
        self.assertEqual(calls["workspace_status"], 0)

    async def test_ensure_runtime_ready_requires_healthy_bridge(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.sio = SimpleNamespace(connected=True)
        runtime = MaxRuntimeManager(conn)
        runtime.require_healthy_ready = True
        runtime._resolve_host_patch = lambda: Path(__file__)
        runtime._ensure_node_dependencies_sync = lambda: {"ready": True}
        runtime._apply_target_to_bridge = lambda: asyncio.sleep(0, result={"success": True})
        runtime._write_state = lambda _status: None

        async def fake_start_server():
            return True

        conn.start_server = fake_start_server  # type: ignore[method-assign]

        async def fake_send_request(payload, timeout=2.0, **_kwargs):
            action = payload.get("action")
            if action == "health_ping":
                raise MaxMCPError(
                    "TRANSPORT_PROTOCOL_ERROR",
                    "Failed to hand off request through dictionary transport.",
                    recoverable=True,
                    details={"required_transport": TRANSPORT_DICT_REF},
                )
            if action == "workspace_status":
                return {"success": True}
            if action == "set_workspace_target":
                return {"success": True}
            return {"success": True}

        conn.send_request = fake_send_request  # type: ignore[method-assign]
        status = await runtime.ensure_runtime_ready()
        self.assertFalse(status["ready"])
        self.assertIn("dictionary transport", status["error"].lower())

    async def test_runtime_target_switch_without_bridge_connection(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        runtime = MaxRuntimeManager(conn)
        result = await runtime.set_active_target("scratch")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], ERROR_PRECONDITION)

    async def test_runtime_twin_detects_drift_and_auto_resync(self):
        topo_a = {
            "boxes": [{"box": {"varname": "a", "maxclass": "newobj", "numinlets": 1, "numoutlets": 1}}],
            "lines": [],
        }
        topo_b = {
            "boxes": [
                {"box": {"varname": "a", "maxclass": "newobj", "numinlets": 1, "numoutlets": 1}},
                {"box": {"varname": "b", "maxclass": "newobj", "numinlets": 1, "numoutlets": 1}},
            ],
            "lines": [],
        }

        class FakeBridge:
            def __init__(self):
                self.sio = SimpleNamespace(connected=True)
                self._topologies = [topo_a, topo_b, topo_b]
                self._idx = 0

            async def send_request(self, payload, timeout=2.0):
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    i = min(self._idx, len(self._topologies) - 1)
                    self._idx += 1
                    return self._topologies[i]
                if action == "get_patcher_context":
                    return {"depth": 0, "path": [], "is_root": True}
                return {}

        runtime = MaxRuntimeManager(FakeBridge())
        sync_result = await runtime.sync_patch_twin(reason="unit-test")
        self.assertTrue(sync_result["success"])
        drift = await runtime.check_patch_drift(auto_resync=True)
        self.assertTrue(drift["success"])
        self.assertFalse(drift["in_sync"])
        self.assertTrue(drift["auto_resync"]["success"])
        self.assertEqual(runtime.twin_baseline_hash, runtime.twin_last_live_hash)

    async def test_runtime_checkpoint_restore_roundtrip(self):
        topo = {
            "boxes": [{"box": {"varname": "x", "maxclass": "newobj", "numinlets": 1, "numoutlets": 1}}],
            "lines": [],
        }

        class FakeBridge:
            def __init__(self):
                self.sio = SimpleNamespace(connected=True)
                self.applied_snapshot = None

            async def send_request(self, payload, timeout=2.0):
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    return topo
                if action == "get_patcher_context":
                    return {"depth": 0, "path": [], "is_root": True}
                if action == "apply_topology_snapshot":
                    self.applied_snapshot = payload.get("snapshot")
                    return {"success": True}
                return {}

        bridge = FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        _seed_workspace(runtime)
        ckpt = await runtime.create_checkpoint(label="test")
        self.assertTrue(ckpt["success"])
        restored = await runtime.restore_checkpoint(ckpt["checkpoint_id"])
        self.assertTrue(restored["success"])
        self.assertEqual(bridge.applied_snapshot, topo)

    async def test_runtime_checkpoint_blocked_on_host_target(self):
        class FakeBridge:
            def __init__(self):
                self.sio = SimpleNamespace(connected=True)

            async def send_request(self, _payload, timeout=2.0):
                raise AssertionError("send_request should not be called for host target checkpoint")

        runtime = MaxRuntimeManager(FakeBridge())
        runtime.active_target = "host"
        result = await runtime.create_checkpoint(label="host-guard")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], ERROR_PRECONDITION)

    async def test_workspace_target_switch_accepts_scoped_target(self):
        class FakeBridge:
            def __init__(self):
                self.sio = SimpleNamespace(connected=True)
                self.current_target = "host"

            async def send_request(
                self,
                payload,
                timeout=2.0,
                idempotency_key=None,
                include_envelope=False,
            ):
                action = payload.get("action")
                if action == "set_workspace_target":
                    self.current_target = payload.get("target_id", "host")
                    return {"success": True, "target_id": self.current_target}
                return {"success": True}

        bridge = FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.register_project(
            project_id=TEST_PROJECT_ID,
            create_default_workspace=False,
        )
        runtime.create_workspace(
            project_id=TEST_PROJECT_ID,
            workspace_id=TEST_WORKSPACE_ID,
        )

        switch_result = await runtime.set_active_target(TEST_SCOPE)
        self.assertTrue(switch_result["success"])
        self.assertEqual(switch_result["target_id"], TEST_SCOPE)
        self.assertEqual(runtime.active_target, TEST_SCOPE)
        self.assertEqual(bridge.current_target, TEST_SCOPE)

    async def test_checkpoint_journal_roundtrip(self):
        topology = {
            "boxes": [{"box": {"varname": "x", "maxclass": "newobj", "numinlets": 1, "numoutlets": 1}}],
            "lines": [],
        }

        class FakeBridge:
            def __init__(self):
                self.sio = SimpleNamespace(connected=True)

            async def send_request(self, payload, timeout=2.0):
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    return topology
                if action == "get_patcher_context":
                    return {"depth": 0, "path": [], "is_root": True}
                return {"success": True}

        bridge = FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        _seed_workspace(runtime)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime.session_dir = tmp_path
            runtime.session_active_patch = tmp_path / "active.maxpat"
            runtime.session_scratch_patch = tmp_path / "scratch.maxpat"
            runtime.checkpoints_file = tmp_path / "checkpoints.json"
            runtime._ensure_session_patches_sync()

            created = await runtime.create_checkpoint(label="persist-me")
            self.assertTrue(created["success"])
            self.assertTrue(runtime.checkpoints_file.exists())

            runtime2 = MaxRuntimeManager(bridge)
            runtime2.session_dir = tmp_path
            runtime2.session_active_patch = tmp_path / "active.maxpat"
            runtime2.session_scratch_patch = tmp_path / "scratch.maxpat"
            runtime2.checkpoints_file = tmp_path / "checkpoints.json"
            loaded = runtime2._load_checkpoint_journal_sync()
            self.assertTrue(loaded["loaded"])
            checkpoints = runtime2.list_checkpoints()
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0]["checkpoint_id"], created["checkpoint_id"])

    async def test_persist_workspace_target_retries_after_timeout(self):
        class FlakyBridge:
            def __init__(self):
                self.sio = SimpleNamespace(connected=True)
                self.calls = 0
                self.workspace_capture_timeouts = 0
                self.workspace_capture_retries = 0

            async def send_request(self, payload, timeout=2.0, idempotency_key=None, include_envelope=False):
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    self.calls += 1
                    if self.calls < 3:
                        raise MaxMCPError(
                            ERROR_BRIDGE_TIMEOUT,
                            "No response received in 8.0 seconds.",
                            recoverable=True,
                            details={},
                        )
                    return {"boxes": [], "lines": []}
                if action == "get_patcher_context":
                    return {"depth": 0, "path": [], "is_root": True}
                return {"success": True}

        bridge = FlakyBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.workspace_capture_timeout_seconds = 0.01
        runtime.workspace_capture_retries = 2
        runtime.workspace_capture_backoff_seconds = 0.0

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime.session_dir = tmp_path
            runtime.session_active_patch = tmp_path / "active.maxpat"
            runtime.session_scratch_patch = tmp_path / "scratch.maxpat"
            runtime.checkpoints_file = tmp_path / "checkpoints.json"
            runtime._ensure_session_patches_sync()

            result = await runtime.persist_workspace_target(target="active", reason="retry-test")
            self.assertTrue(result["persisted"])
            self.assertEqual(result["capture"]["attempts"], 3)
            self.assertEqual(result["capture"]["retry_attempts"], 2)
            self.assertEqual(bridge.workspace_capture_timeouts, 2)
            self.assertEqual(bridge.workspace_capture_retries, 2)


class PatchFileFlowTests(unittest.IsolatedAsyncioTestCase):
    class FakeBridge:
        def __init__(self):
            self.sio = SimpleNamespace(connected=True)
            self.current_target = "active"
            self.progressive_calls = 0
            self.strict_capability_gating = False
            self.capabilities = {
                "supported_actions": [
                    "set_workspace_target",
                    "get_objects_in_patch",
                    "get_patcher_context",
                    "apply_topology_snapshot",
                    "apply_topology_snapshot_progressive",
                ]
            }
            self.topologies = {
                "active": {"boxes": [], "lines": []},
                "scratch": {"boxes": [], "lines": []},
                TEST_SCOPE: {"boxes": [], "lines": []},
            }

        async def send_request(
            self,
            payload,
            timeout=2.0,
            idempotency_key=None,
            include_envelope=False,
        ):
            action = payload.get("action")
            if action == "set_workspace_target":
                self.current_target = payload.get("target_id", "active")
                return {"success": True, "target_id": self.current_target}
            if action == "capabilities":
                return self.capabilities
            if action == "get_objects_in_patch":
                return json.loads(
                    json.dumps(self.topologies.get(self.current_target, {"boxes": [], "lines": []}))
                )
            if action == "get_patcher_context":
                return {"depth": 0, "path": [], "is_root": True}
            if action == "apply_topology_snapshot":
                snapshot = json.loads(json.dumps(payload.get("snapshot", {"boxes": [], "lines": []})))
                self.topologies[self.current_target] = snapshot
                return {
                    "success": True,
                    "restored_boxes": len(snapshot.get("boxes", [])),
                    "restored_lines": len(snapshot.get("lines", [])),
                    "skipped_boxes": 0,
                    "skipped_lines": 0,
                }
            if action == "apply_topology_snapshot_progressive":
                snapshot = json.loads(json.dumps(payload.get("snapshot", {"boxes": [], "lines": []})))
                self.topologies[self.current_target] = snapshot
                self.progressive_calls += 1
                return {
                    "success": True,
                    "done": True,
                    "restored_boxes": len(snapshot.get("boxes", [])),
                    "restored_lines": len(snapshot.get("lines", [])),
                    "skipped_boxes": 0,
                    "skipped_lines": 0,
                    "chunks_processed": 1,
                }
            if action == "health_ping":
                return {"ok": True}
            if action == "workspace_status":
                return {"success": True, "target_id": self.current_target}
            return {"success": True}

    async def test_validate_patch_file_success_and_strict(self):
        runtime = MaxRuntimeManager(self.FakeBridge())
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            valid_path = tmp_path / "valid.maxpat"
            valid_path.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj", "varname": "v1"}}],
                            "lines": [],
                        }
                    }
                )
            )
            result = await runtime.validate_patch_file(str(valid_path))
            self.assertTrue(result["success"])
            self.assertEqual(result["detected_format"], "maxpat_patcher")

            strict_path = tmp_path / "strict.maxpat"
            strict_path.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj"}}],
                            "lines": [],
                        }
                    }
                )
            )
            strict = await runtime.validate_patch_file(str(strict_path), strict=True)
            self.assertFalse(strict["success"])
            self.assertEqual(strict["error"]["code"], "VALIDATION_ERROR")

    async def test_import_patch_replace(self):
        bridge = self.FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "source.maxpat"
            source_payload = {
                "patcher": {
                    "boxes": [
                        {
                            "box": {
                                "maxclass": "newobj",
                                "varname": "osc1",
                                "boxtext": "cycle~ 220",
                            }
                        }
                    ],
                    "lines": [],
                }
            }
            source_path.write_text(json.dumps(source_payload))

            loaded = await runtime.import_patch(
                path=str(source_path),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                mode="replace",
                create_checkpoint_before_load=False,
            )
            self.assertTrue(loaded["success"])
            self.assertEqual(bridge.topologies[TEST_SCOPE]["boxes"][0]["box"]["varname"], "osc1")

    async def test_import_patch_remaps_line_refs_from_box_ids(self):
        bridge = self.FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "id_refs.maxpat"
            source_payload = {
                "patcher": {
                    "boxes": [
                        {"box": {"id": "obj-1", "maxclass": "newobj", "boxtext": "cycle~ 220"}},
                        {"box": {"id": "obj-2", "maxclass": "newobj", "boxtext": "dac~"}},
                    ],
                    "lines": [
                        {"patchline": {"source": ["obj-1", 0], "destination": ["obj-2", 0]}},
                    ],
                }
            }
            source_path.write_text(json.dumps(source_payload))

            loaded = await runtime.import_patch(
                path=str(source_path),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                mode="replace",
                create_checkpoint_before_load=False,
            )
            self.assertTrue(loaded["success"])
            self.assertEqual(loaded["import_summary"]["generated_varnames"], 2)
            self.assertGreaterEqual(loaded["import_summary"]["line_ref_id_mappings"], 2)
            self.assertEqual(len(bridge.topologies[TEST_SCOPE]["lines"]), 1)
            line = bridge.topologies[TEST_SCOPE]["lines"][0]["patchline"]
            valid_vars = {
                row["box"]["varname"]
                for row in bridge.topologies[TEST_SCOPE]["boxes"]
                if isinstance(row, dict) and isinstance(row.get("box"), dict)
            }
            self.assertIn(line["source"][0], valid_vars)
            self.assertIn(line["destination"][0], valid_vars)

    async def test_import_patch_merge_and_fail_if_not_empty(self):
        bridge = self.FakeBridge()
        bridge.topologies[TEST_SCOPE] = {
            "boxes": [{"box": {"maxclass": "newobj", "varname": "foo"}}],
            "lines": [],
        }
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "merge.maxpat"
            source_path.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj", "varname": "foo"}}],
                            "lines": [],
                        }
                    }
                )
            )

            fail_nonempty = await runtime.import_patch(
                path=str(source_path),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                mode="fail_if_not_empty",
                create_checkpoint_before_load=False,
            )
            self.assertFalse(fail_nonempty["success"])
            self.assertEqual(fail_nonempty["error"]["code"], "PRECONDITION_FAILED")

            merged = await runtime.import_patch(
                path=str(source_path),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                mode="merge",
                auto_rename_collisions=True,
                create_checkpoint_before_load=False,
            )
            self.assertTrue(merged["success"])
            self.assertEqual(merged["import_summary"]["collisions_count"], 1)
            self.assertEqual(len(bridge.topologies[TEST_SCOPE]["boxes"]), 2)
            remap = merged["import_summary"]["varname_remap"]
            self.assertIn("foo", remap)
            self.assertNotEqual(remap["foo"], "foo")

    async def test_export_workspace_writes_payload(self):
        bridge = self.FakeBridge()
        bridge.topologies[TEST_SCOPE] = {
            "boxes": [{"box": {"maxclass": "newobj", "varname": "out1"}}],
            "lines": [],
        }
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})
        runtime.register_project(
            project_id=TEST_PROJECT_ID,
            create_default_workspace=False,
        )
        runtime.create_workspace(
            project_id=TEST_PROJECT_ID,
            workspace_id=TEST_WORKSPACE_ID,
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "export.maxpat"
            saved = await runtime.export_workspace(
                path=str(output),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
            )
            self.assertTrue(saved["success"])
            self.assertTrue(output.exists())
            payload = json.loads(output.read_text())
            self.assertIn("patcher", payload)
            self.assertEqual(payload["patcher"]["boxes"][0]["box"]["varname"], "out1")

    async def test_import_patch_uses_progressive_apply_in_auto_mode(self):
        bridge = self.FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "source.maxpat"
            source_path.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj", "varname": "a"}}],
                            "lines": [],
                        }
                    }
                )
            )
            loaded = await runtime.import_patch(
                path=str(source_path),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                apply_mode="auto",
                create_checkpoint_before_load=False,
            )
            self.assertTrue(loaded["success"])
            self.assertEqual(loaded["import_summary"]["apply_mode_selected"], "progressive")
            self.assertEqual(bridge.progressive_calls, 1)

    async def test_import_patch_retries_progressive_timeout(self):
        class FlakyProgressiveBridge(self.FakeBridge):
            def __init__(self):
                super().__init__()
                self._progressive_attempts = 0

            async def send_request(
                self,
                payload,
                timeout=2.0,
                idempotency_key=None,
                include_envelope=False,
            ):
                action = payload.get("action")
                if action == "apply_topology_snapshot_progressive":
                    self._progressive_attempts += 1
                    if self._progressive_attempts == 1:
                        raise MaxMCPError(
                            ERROR_BRIDGE_TIMEOUT,
                            "No response received in 1.0 seconds.",
                            recoverable=True,
                            details={},
                        )
                return await super().send_request(
                    payload,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                    include_envelope=include_envelope,
                )

        bridge = FlakyProgressiveBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "source.maxpat"
            source_path.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj", "varname": "a"}}],
                            "lines": [],
                        }
                    }
                )
            )
            loaded = await runtime.import_patch(
                path=str(source_path),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                apply_mode="progressive",
                apply_retry_count=1,
                apply_retry_backoff_seconds=0.0,
                create_checkpoint_before_load=False,
            )
            self.assertTrue(loaded["success"])
            self.assertEqual(loaded["apply_result"]["attempt"], 2)
            self.assertEqual(len(loaded["apply_meta"]["attempts_failed"]), 1)

    async def test_progressive_apply_uses_remaining_timeout_budget(self):
        class BudgetBridge(self.FakeBridge):
            def __init__(self):
                super().__init__()
                self.progressive_timeout_args = []
                self._progressive_calls = 0

            async def send_request(
                self,
                payload,
                timeout=2.0,
                idempotency_key=None,
                include_envelope=False,
            ):
                if payload.get("action") == "apply_topology_snapshot_progressive":
                    _ = idempotency_key
                    _ = include_envelope
                    self._progressive_calls += 1
                    self.progressive_timeout_args.append(float(timeout))
                    await asyncio.sleep(0.05)
                    if self._progressive_calls == 1:
                        return {"done": False, "state": {"cursor": 1}}
                    return {"done": True, "success": True, "chunks_processed": 2}
                return await super().send_request(
                    payload,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                    include_envelope=include_envelope,
                )

        bridge = BudgetBridge()
        runtime = MaxRuntimeManager(bridge)

        result = await runtime._apply_topology_snapshot_progressive(
            {"boxes": [{"box": {"maxclass": "newobj", "varname": "a"}}], "lines": []},
            timeout_seconds=0.20,
            chunk_size=1,
        )
        self.assertTrue(result.get("done"))
        self.assertEqual(len(bridge.progressive_timeout_args), 2)
        self.assertLess(
            bridge.progressive_timeout_args[1],
            bridge.progressive_timeout_args[0],
        )

    async def test_progressive_apply_enforces_total_timeout_budget(self):
        class SlowBridge(self.FakeBridge):
            async def send_request(
                self,
                payload,
                timeout=2.0,
                idempotency_key=None,
                include_envelope=False,
            ):
                if payload.get("action") == "apply_topology_snapshot_progressive":
                    _ = timeout
                    _ = idempotency_key
                    _ = include_envelope
                    await asyncio.sleep(0.035)
                    return {"done": False, "state": {"cursor": 1}}
                return await super().send_request(
                    payload,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                    include_envelope=include_envelope,
                )

        bridge = SlowBridge()
        runtime = MaxRuntimeManager(bridge)

        with self.assertRaises(MaxMCPError) as ctx:
            await runtime._apply_topology_snapshot_progressive(
                {"boxes": [{"box": {"maxclass": "newobj", "varname": "a"}}], "lines": []},
                timeout_seconds=0.06,
                chunk_size=1,
            )
        self.assertEqual(ctx.exception.code, ERROR_BRIDGE_TIMEOUT)
        self.assertIn("timeout budget", ctx.exception.message.lower())
        self.assertIn("elapsed_seconds", ctx.exception.details)

    async def test_import_patch_capability_gating_preflight(self):
        bridge = self.FakeBridge()
        bridge.strict_capability_gating = True
        bridge.capabilities = {"supported_actions": ["get_objects_in_patch"]}
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.maxpat"
            source.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj", "varname": "a"}}],
                            "lines": [],
                        }
                    }
                )
            )
            result = await runtime.import_patch(
                path=str(source),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                apply_mode="single",
                create_checkpoint_before_load=False,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_PRECONDITION)
            self.assertIn("missing_actions", result["error"]["details"])

    async def test_import_patch_overloaded_apply_returns_structured_error(self):
        class OverloadedBridge(self.FakeBridge):
            async def send_request(
                self,
                payload,
                timeout=2.0,
                idempotency_key=None,
                include_envelope=False,
            ):
                action = payload.get("action")
                if action == "apply_topology_snapshot":
                    raise MaxMCPError(
                        ERROR_OVERLOADED,
                        "Mutation queue is full.",
                        recoverable=True,
                        details={"queued": 64, "inflight": 4},
                    )
                return await super().send_request(
                    payload,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                    include_envelope=include_envelope,
                )

        bridge = OverloadedBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.maxpat"
            source.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj", "varname": "a"}}],
                            "lines": [],
                        }
                    }
                )
            )
            result = await runtime.import_patch(
                path=str(source),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                apply_mode="single",
                create_checkpoint_before_load=False,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_OVERLOADED)
            self.assertEqual(result["error"]["details"]["action"], "apply_topology_snapshot")
            self.assertEqual(result["error"]["details"]["operation"], "import_patch")

    async def test_export_workspace_unauthorized_returns_structured_error(self):
        class UnauthorizedBridge(self.FakeBridge):
            async def send_request(
                self,
                payload,
                timeout=2.0,
                idempotency_key=None,
                include_envelope=False,
            ):
                action = payload.get("action")
                if action == "get_objects_in_patch":
                    raise MaxMCPError(
                        ERROR_UNAUTHORIZED,
                        "Unauthorized request.",
                        recoverable=False,
                    )
                return await super().send_request(
                    payload,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                    include_envelope=include_envelope,
                )

        bridge = UnauthorizedBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})
        runtime.register_project(
            project_id=TEST_PROJECT_ID,
            create_default_workspace=False,
        )
        runtime.create_workspace(
            project_id=TEST_PROJECT_ID,
            workspace_id=TEST_WORKSPACE_ID,
        )

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "export.maxpat"
            result = await runtime.export_workspace(
                path=str(output),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_UNAUTHORIZED)
            self.assertEqual(result["error"]["details"]["action"], "get_objects_in_patch")
            self.assertEqual(result["error"]["details"]["operation"], "export_workspace")

    async def test_import_patch_rejects_disallowed_read_root(self):
        bridge = self.FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})
        runtime.enforce_patch_roots = True

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            allowed = tmp_path / "allowed"
            blocked = tmp_path / "blocked"
            allowed.mkdir()
            blocked.mkdir()
            runtime.allowed_patch_roots = [allowed.resolve()]

            source = blocked / "source.maxpat"
            source.write_text(
                json.dumps(
                    {
                        "patcher": {
                            "boxes": [{"box": {"maxclass": "newobj", "varname": "a"}}],
                            "lines": [],
                        }
                    }
                )
            )

            result = await runtime.import_patch(
                path=str(source),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                create_checkpoint_before_load=False,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_PRECONDITION)
            self.assertEqual(result["error"]["details"]["purpose"], "patch_read")

    async def test_export_workspace_rejects_disallowed_write_root(self):
        bridge = self.FakeBridge()
        bridge.topologies[TEST_SCOPE] = {
            "boxes": [{"box": {"maxclass": "newobj", "varname": "out1"}}],
            "lines": [],
        }
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})
        runtime.enforce_patch_roots = True
        runtime.register_project(
            project_id=TEST_PROJECT_ID,
            create_default_workspace=False,
        )
        runtime.create_workspace(
            project_id=TEST_PROJECT_ID,
            workspace_id=TEST_WORKSPACE_ID,
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            allowed = tmp_path / "allowed"
            blocked = tmp_path / "blocked"
            allowed.mkdir()
            blocked.mkdir()
            runtime.allowed_patch_roots = [allowed.resolve()]

            output = blocked / "export.maxpat"
            result = await runtime.export_workspace(
                path=str(output),
                project_id=TEST_PROJECT_ID,
                workspace_id=TEST_WORKSPACE_ID,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_PRECONDITION)
            self.assertEqual(result["error"]["details"]["purpose"], "patch_write")


class TransactionBuilderTests(unittest.TestCase):
    def test_transaction_builder_maps_add_object(self):
        payload, timeout = _build_transaction_bridge_request(
            1,
            "add_max_object",
            {
                "position": [10, 10],
                "obj_type": "scale",
                "varname": "v1",
                "args": ["0", "127", "0", "1."],
            },
        )
        self.assertEqual(payload["action"], "add_object")
        self.assertEqual(payload["obj_type"], "scale")
        self.assertEqual(timeout, 8.0)

    def test_transaction_builder_maps_newobj_shorthand(self):
        payload, _timeout = _build_transaction_bridge_request(
            1,
            "add_max_object",
            {
                "position": [10, 10],
                "obj_type": "newobj",
                "varname": "v1",
                "args": ["prepend", "set"],
            },
        )
        self.assertEqual(payload["obj_type"], "prepend")
        self.assertEqual(payload["args"], ["set"])

    def test_transaction_builder_rejects_unknown_action(self):
        with self.assertRaises(MaxMCPError):
            _build_transaction_bridge_request(1, "nonexistent_action", {})


class TransactionExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_transaction_rolls_back_on_failure(self):
        class FakeBridge:
            def __init__(self):
                self.sio = SimpleNamespace(connected=True)
                self.applied_snapshot = None

            async def send_request(
                self,
                payload,
                timeout=2.0,
                idempotency_key=None,
                include_envelope=False,
            ):
                action = payload.get("action")
                if action == "get_patcher_context":
                    return {"depth": 0, "path": [], "is_root": True}
                if action == "get_objects_in_patch":
                    return {"boxes": [], "lines": []}
                if action == "add_object":
                    return {"success": True}
                if action == "remove_object":
                    return {
                        "success": False,
                        "error": {
                            "code": "OBJECT_NOT_FOUND",
                            "message": "Object not found",
                            "recoverable": True,
                            "details": {},
                        },
                    }
                if action == "apply_topology_snapshot":
                    self.applied_snapshot = payload.get("snapshot")
                    return {"success": True}
                return {"success": True}

        bridge = FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        _seed_workspace(runtime)
        ctx = _make_scoped_ctx(bridge, runtime=runtime)
        steps = [
            {
                "action": "add_max_object",
                "params": {
                    "position": [10, 10],
                    "obj_type": "button",
                    "varname": "tx_obj",
                    "args": [],
                },
            },
            {
                "action": "remove_max_object",
                "params": {"varname": "tx_obj"},
            },
        ]
        result = await run_patch_transaction(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            steps=steps,
            dry_run_engine="maxpy",
            rollback_on_error=True,
            checkpoint_label="tx-test",
        )
        self.assertFalse(result["success"])
        self.assertIsNotNone(result.get("rollback"))
        self.assertTrue(result["rollback"]["success"])
        self.assertEqual(bridge.applied_snapshot, {"boxes": [], "lines": []})

    async def test_transaction_requires_existing_workspace_scope(self):
        class FakeBridge:
            def __init__(self):
                self.sio = SimpleNamespace(connected=True)

            async def send_request(
                self,
                payload,
                timeout=2.0,
                idempotency_key=None,
                include_envelope=False,
            ):
                raise AssertionError("transaction should fail before any bridge calls")

        bridge = FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        ctx = _make_scoped_ctx(bridge, runtime=runtime)
        result = await run_patch_transaction(
            ctx,
            TEST_PROJECT_ID,
            TEST_WORKSPACE_ID,
            steps=[],
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], ERROR_VALIDATION)


if __name__ == "__main__":
    unittest.main()
