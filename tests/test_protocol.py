import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import server as server_module
from server import (
    ERROR_BRIDGE_TIMEOUT,
    ERROR_INTERNAL,
    ERROR_OVERLOADED,
    ERROR_PRECONDITION,
    ERROR_UNKNOWN_ACTION,
    ERROR_UNAUTHORIZED,
    MaxMCPError,
    MaxMSPConnection,
    MaxRuntimeManager,
    _normalize_add_object_spec,
    _normalize_avoid_rect_payload,
    _build_transaction_bridge_request,
    _resolve_auth_token_from_sources,
    add_max_object,
    get_avoid_rect_position,
    maxpy_catalog,
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
        self.assertEqual(ctx.exception.code, ERROR_PRECONDITION)

    async def test_send_request_accepts_legacy_response_when_strict_disabled(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.strict_v2_enforcement = False

        async def handler(_event, payload, _namespace):
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result({"request_id": req_id, "results": {"legacy": True}})

        conn.sio = FakeSocketClient(handler=handler)
        result = await conn.send_request({"action": "health_ping"}, timeout=1.0)
        self.assertEqual(result, {"legacy": True})

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

    async def test_send_request_timeout_maps_to_bridge_timeout_error(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.sio = FakeSocketClient(handler=None)
        with self.assertRaises(MaxMCPError) as ctx:
            await conn.send_request({"action": "health_ping"}, timeout=0.01)
        self.assertEqual(ctx.exception.code, ERROR_BRIDGE_TIMEOUT)

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
            seen.append(payload.get("varname"))
            await asyncio.sleep(0.02)
            req_id = payload["request_id"]
            fut = conn._pending[req_id]
            fut.set_result(
                {
                    "protocol_version": "2.0",
                    "request_id": req_id,
                    "state": "succeeded",
                    "results": {"ok": True, "varname": payload.get("varname")},
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


class DryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_validates_and_tracks_virtual_depth(self):
        fake_conn = SimpleNamespace()

        async def fake_send_request(_payload, timeout=2.0):
            return {"depth": 0, "path": [], "is_root": True}

        fake_conn.send_request = fake_send_request
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"maxmsp": fake_conn}
            )
        )
        plan = [
            {"action": "enter_subpatcher", "params": {"varname": "p1"}},
            {"action": "add_max_object", "params": {"position": [0, 0], "obj_type": "+", "varname": "n1", "args": [0]}},
            {"action": "exit_subpatcher", "params": {}},
        ]
        result = await dry_run_plan(ctx, plan)
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
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"maxmsp": fake_conn}
            )
        )

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
        result = await dry_run_plan(ctx, plan, engine="maxpy")
        self.assertFalse(result["valid"])
        self.assertEqual(result["engine"], "maxpy")
        self.assertGreaterEqual(len(result["errors"]), 1)

    async def test_dry_run_unknown_action_defaults_to_error(self):
        fake_conn = SimpleNamespace()

        async def fake_send_request(_payload, timeout=2.0):
            return {"depth": 0, "path": [], "is_root": True}

        fake_conn.send_request = fake_send_request
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context={"maxmsp": fake_conn})
        )
        result = await dry_run_plan(
            ctx,
            steps=[{"action": "totally_new_action", "params": {}}],
        )
        self.assertFalse(result["valid"])
        self.assertTrue(any(err.get("code") == ERROR_UNKNOWN_ACTION for err in result["errors"]))

    async def test_dry_run_unknown_action_warn_policy(self):
        fake_conn = SimpleNamespace()

        async def fake_send_request(_payload, timeout=2.0):
            return {"depth": 0, "path": [], "is_root": True}

        fake_conn.send_request = fake_send_request
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context={"maxmsp": fake_conn})
        )
        result = await dry_run_plan(
            ctx,
            steps=[{"action": "totally_new_action", "params": {}}],
            unknown_action_policy="warn",
        )
        self.assertTrue(result["valid"])
        self.assertTrue(any(warn.get("code") == ERROR_UNKNOWN_ACTION for warn in result["warnings"]))


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
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context={"maxmsp": bridge})
        )
        original_mode = server_module.MAXMCP_PREFLIGHT_MODE
        try:
            server_module.MAXMCP_PREFLIGHT_MODE = "auto"
            result = await add_max_object(
                ctx,
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
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context={"maxmsp": bridge})
        )
        rect = await get_avoid_rect_position(ctx)
        self.assertEqual(rect, [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(bridge.preflight_invalid_rects, 1)


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


class RuntimeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_target_switch_without_bridge_connection(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        runtime = MaxRuntimeManager(conn)
        result = await runtime.set_active_target("scratch")
        self.assertTrue(result["success"])
        self.assertEqual(result["active_target"], "scratch")
        self.assertFalse(result["apply_result"]["applied"])
        self.assertEqual(result["apply_result"]["reason"], "bridge_disconnected")

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

    async def test_workspace_target_switch_persists_and_hydrates_workspace_files(self):
        active_topology = {
            "boxes": [
                {
                    "box": {
                        "varname": "active_osc",
                        "maxclass": "newobj",
                        "patching_rect": [10.0, 10.0, 90.0, 32.0],
                        "numinlets": 2,
                        "numoutlets": 1,
                        "boxtext": "cycle~ 440",
                        "attributes": {"comment": "active"},
                    }
                }
            ],
            "lines": [],
        }
        scratch_topology = {
            "boxes": [
                {
                    "box": {
                        "varname": "scratch_gain",
                        "maxclass": "newobj",
                        "patching_rect": [20.0, 20.0, 100.0, 42.0],
                        "numinlets": 2,
                        "numoutlets": 1,
                        "boxtext": "gain~",
                        "attributes": {"comment": "scratch"},
                    }
                }
            ],
            "lines": [],
        }

        class FakeBridge:
            def __init__(self):
                self.sio = SimpleNamespace(connected=True)
                self.current_target = "active"
                self.topologies = {
                    "active": json.loads(json.dumps(active_topology)),
                    "scratch": {"boxes": [], "lines": []},
                    "host": {"boxes": [], "lines": []},
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
                    self.current_target = payload.get("target_id", "host")
                    return {"success": True, "target_id": self.current_target}
                if action == "get_objects_in_patch":
                    return json.loads(
                        json.dumps(self.topologies.get(self.current_target, {"boxes": [], "lines": []}))
                    )
                if action == "apply_topology_snapshot":
                    snapshot = json.loads(json.dumps(payload.get("snapshot", {"boxes": [], "lines": []})))
                    if self.current_target in {"active", "scratch"}:
                        self.topologies[self.current_target] = snapshot
                    return {"success": True}
                if action == "get_patcher_context":
                    return {"depth": 0, "path": [], "is_root": True}
                return {"success": True}

        bridge = FakeBridge()
        runtime = MaxRuntimeManager(bridge)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime.session_dir = tmp_path
            runtime.session_active_patch = tmp_path / "active.maxpat"
            runtime.session_scratch_patch = tmp_path / "scratch.maxpat"
            runtime.checkpoints_file = tmp_path / "checkpoints.json"
            runtime._ensure_session_patches_sync()

            switch_one = await runtime.set_active_target("scratch")
            self.assertTrue(switch_one["success"])
            active_payload = json.loads(runtime.session_active_patch.read_text())
            active_from_file = runtime._extract_topology_from_payload(active_payload)
            self.assertEqual(active_from_file["boxes"][0]["box"]["varname"], "active_osc")

            bridge.topologies["scratch"] = json.loads(json.dumps(scratch_topology))
            switch_two = await runtime.set_active_target("active")
            self.assertTrue(switch_two["success"])
            scratch_payload = json.loads(runtime.session_scratch_patch.read_text())
            scratch_from_file = runtime._extract_topology_from_payload(scratch_payload)
            self.assertEqual(scratch_from_file["boxes"][0]["box"]["varname"], "scratch_gain")
            self.assertEqual(bridge.current_target, "active")
            self.assertEqual(bridge.topologies["active"]["boxes"][0]["box"]["varname"], "active_osc")

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
            self.topologies = {
                "active": {"boxes": [], "lines": []},
                "scratch": {"boxes": [], "lines": []},
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

    async def test_load_patch_from_path_replace_and_host_guard(self):
        bridge = self.FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime.session_dir = tmp_path
            runtime.session_active_patch = tmp_path / "active.maxpat"
            runtime.session_scratch_patch = tmp_path / "scratch.maxpat"
            runtime.checkpoints_file = tmp_path / "checkpoints.json"
            runtime._ensure_session_patches_sync()

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

            host_reject = await runtime.load_patch_from_path(
                str(source_path),
                target="host",
                create_checkpoint_before_load=False,
            )
            self.assertFalse(host_reject["success"])

            loaded = await runtime.load_patch_from_path(
                str(source_path),
                target="active",
                mode="replace",
                create_checkpoint_before_load=False,
            )
            self.assertTrue(loaded["success"])
            self.assertEqual(bridge.topologies["active"]["boxes"][0]["box"]["varname"], "osc1")

    async def test_load_patch_from_path_remaps_line_refs_from_box_ids(self):
        bridge = self.FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime.session_dir = tmp_path
            runtime.session_active_patch = tmp_path / "active.maxpat"
            runtime.session_scratch_patch = tmp_path / "scratch.maxpat"
            runtime.checkpoints_file = tmp_path / "checkpoints.json"
            runtime._ensure_session_patches_sync()

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

            loaded = await runtime.load_patch_from_path(
                str(source_path),
                target="active",
                mode="replace",
                create_checkpoint_before_load=False,
            )
            self.assertTrue(loaded["success"])
            self.assertEqual(loaded["import_summary"]["generated_varnames"], 2)
            self.assertGreaterEqual(loaded["import_summary"]["line_ref_id_mappings"], 2)
            self.assertEqual(len(bridge.topologies["active"]["lines"]), 1)
            line = bridge.topologies["active"]["lines"][0]["patchline"]
            valid_vars = {
                row["box"]["varname"]
                for row in bridge.topologies["active"]["boxes"]
                if isinstance(row, dict) and isinstance(row.get("box"), dict)
            }
            self.assertIn(line["source"][0], valid_vars)
            self.assertIn(line["destination"][0], valid_vars)

    async def test_load_patch_from_path_merge_and_fail_if_not_empty(self):
        bridge = self.FakeBridge()
        bridge.topologies["active"] = {
            "boxes": [{"box": {"maxclass": "newobj", "varname": "foo"}}],
            "lines": [],
        }
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime.session_dir = tmp_path
            runtime.session_active_patch = tmp_path / "active.maxpat"
            runtime.session_scratch_patch = tmp_path / "scratch.maxpat"
            runtime.checkpoints_file = tmp_path / "checkpoints.json"
            runtime._ensure_session_patches_sync()

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

            fail_nonempty = await runtime.load_patch_from_path(
                str(source_path),
                target="active",
                mode="fail_if_not_empty",
                create_checkpoint_before_load=False,
            )
            self.assertFalse(fail_nonempty["success"])
            self.assertEqual(fail_nonempty["error"]["code"], "PRECONDITION_FAILED")

            merged = await runtime.load_patch_from_path(
                str(source_path),
                target="active",
                mode="merge",
                auto_rename_collisions=True,
                create_checkpoint_before_load=False,
            )
            self.assertTrue(merged["success"])
            self.assertEqual(merged["import_summary"]["collisions_count"], 1)
            self.assertEqual(len(bridge.topologies["active"]["boxes"]), 2)
            remap = merged["import_summary"]["varname_remap"]
            self.assertIn("foo", remap)
            self.assertNotEqual(remap["foo"], "foo")

    async def test_save_patch_to_path_writes_payload(self):
        bridge = self.FakeBridge()
        bridge.topologies["active"] = {
            "boxes": [{"box": {"maxclass": "newobj", "varname": "out1"}}],
            "lines": [],
        }
        runtime = MaxRuntimeManager(bridge)
        runtime.ensure_runtime_ready = lambda: asyncio.sleep(0, result={"ready": True})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "export.maxpat"
            saved = await runtime.save_patch_to_path(str(output), target="active")
            self.assertTrue(saved["success"])
            self.assertTrue(output.exists())
            payload = json.loads(output.read_text())
            self.assertIn("patcher", payload)
            self.assertEqual(payload["patcher"]["boxes"][0]["box"]["varname"], "out1")

    async def test_load_patch_from_path_capability_gating_preflight(self):
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
            result = await runtime.load_patch_from_path(
                str(source),
                target="active",
                create_checkpoint_before_load=False,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_PRECONDITION)
            self.assertIn("missing_actions", result["error"]["details"])

    async def test_load_patch_from_path_overloaded_apply_returns_structured_error(self):
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
            result = await runtime.load_patch_from_path(
                str(source),
                target="active",
                create_checkpoint_before_load=False,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_OVERLOADED)
            self.assertEqual(result["error"]["details"]["action"], "apply_topology_snapshot")
            self.assertEqual(result["error"]["details"]["operation"], "load_patch_from_path")

    async def test_save_patch_to_path_unauthorized_returns_structured_error(self):
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

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "export.maxpat"
            result = await runtime.save_patch_to_path(str(output), target="active")
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_UNAUTHORIZED)
            self.assertEqual(result["error"]["details"]["action"], "get_objects_in_patch")
            self.assertEqual(result["error"]["details"]["operation"], "save_patch_to_path")

    async def test_load_patch_from_path_rejects_disallowed_read_root(self):
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

            result = await runtime.load_patch_from_path(
                str(source),
                target="active",
                create_checkpoint_before_load=False,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_PRECONDITION)
            self.assertEqual(result["error"]["details"]["purpose"], "patch_read")

    async def test_save_patch_to_path_rejects_disallowed_write_root(self):
        bridge = self.FakeBridge()
        bridge.topologies["active"] = {
            "boxes": [{"box": {"maxclass": "newobj", "varname": "out1"}}],
            "lines": [],
        }
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

            output = blocked / "export.maxpat"
            result = await runtime.save_patch_to_path(str(output), target="active")
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
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"maxmsp": bridge, "runtime": runtime}
            )
        )
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
            steps=steps,
            dry_run_engine="maxpy",
            rollback_on_error=True,
            checkpoint_label="tx-test",
        )
        self.assertFalse(result["success"])
        self.assertIsNotNone(result.get("rollback"))
        self.assertTrue(result["rollback"]["success"])
        self.assertEqual(bridge.applied_snapshot, {"boxes": [], "lines": []})

    async def test_transaction_blocked_on_host_target(self):
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
                raise AssertionError("transaction should fail before bridge calls in host mode")

        bridge = FakeBridge()
        runtime = MaxRuntimeManager(bridge)
        runtime.active_target = "host"
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"maxmsp": bridge, "runtime": runtime}
            )
        )
        result = await run_patch_transaction(ctx, steps=[])
        self.assertFalse(result["success"])
        self.assertEqual(result.get("code"), ERROR_PRECONDITION)


if __name__ == "__main__":
    unittest.main()
