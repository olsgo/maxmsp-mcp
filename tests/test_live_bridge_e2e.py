import asyncio
import os
import time
import unittest

import server


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@unittest.skipUnless(
    _env_bool("MAXMCP_RUN_LIVE_E2E", False),
    "Set MAXMCP_RUN_LIVE_E2E=1 to run live Max bridge E2E tests.",
)
class LiveBridgeE2ETests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = server.MaxMSPConnection(
            server.SOCKETIO_SERVER_URL,
            server.SOCKETIO_SERVER_PORT,
            server.NAMESPACE,
        )
        self.runtime = server.MaxRuntimeManager(self.conn)
        self.conn.runtime_manager = self.runtime
        self._created_varname = None
        self._created_varnames: list[str] = []

    async def asyncTearDown(self):
        if self._created_varname:
            try:
                await self.conn.send_request(
                    {"action": "remove_object", "varname": self._created_varname},
                    timeout=5.0,
                )
            except Exception:
                pass
        if self._created_varnames:
            for varname in list(self._created_varnames):
                try:
                    await self.conn.send_request(
                        {"action": "remove_object", "varname": varname},
                        timeout=5.0,
                    )
                except Exception:
                    pass
        await self.conn.disconnect()

    async def _wait_for_bridge_ready(self, timeout_seconds: float = 30.0):
        started = time.monotonic()
        while time.monotonic() - started < timeout_seconds:
            status = await self.runtime.collect_status(check_bridge=True)
            if status.get("bridge_connected") and status.get("bridge_healthy"):
                return status
            await asyncio.sleep(0.5)
        self.fail("Bridge did not become healthy within timeout.")

    async def test_live_roundtrip_and_metrics(self):
        runtime_status = await self.runtime.ensure_runtime_ready()
        self.assertTrue(runtime_status.get("ready"), runtime_status)
        await self._wait_for_bridge_ready()

        caps = await self.conn.refresh_capabilities()
        self.assertIsInstance(caps, dict)
        self.assertTrue(caps.get("health_ping"))
        supported = caps.get("supported_actions") or []
        self.assertIn("add_object", supported)
        self.assertIn("remove_object", supported)

        ping = await self.conn.send_request({"action": "health_ping"}, timeout=3.0)
        self.assertTrue(ping.get("ok"), ping)

        avoid_rect = await self.conn.send_request(
            {"action": "get_avoid_rect_position"},
            timeout=8.0,
        )
        self.assertIsInstance(avoid_rect, list)
        self.assertGreaterEqual(len(avoid_rect), 4)
        right = avoid_rect[2] if isinstance(avoid_rect[2], (int, float)) else 120
        top = avoid_rect[1] if isinstance(avoid_rect[1], (int, float)) else 120
        x = int(right) + 40
        y = int(top) + 40
        self._created_varname = f"live_e2e_{int(time.time())}"

        add_result = await self.conn.send_request(
            {
                "action": "add_object",
                "position": [x, y],
                "obj_type": "button",
                "args": [],
                "varname": self._created_varname,
            },
            timeout=10.0,
        )
        if isinstance(add_result, dict):
            self.assertTrue(add_result.get("success", True), add_result)

        remove_result = await self.conn.send_request(
            {"action": "remove_object", "varname": self._created_varname},
            timeout=6.0,
        )
        if isinstance(remove_result, dict):
            self.assertTrue(remove_result.get("success", True), remove_result)
        self._created_varname = None

        metrics = self.conn.metrics_snapshot(include_events=True, event_limit=10)
        self.assertGreaterEqual(metrics.get("total_requests", 0), 4)
        self.assertIn("latency_ms", metrics)
        self.assertIn("mutation_queue", metrics)

    async def test_live_short_mutation_soak(self):
        runtime_status = await self.runtime.ensure_runtime_ready()
        self.assertTrue(runtime_status.get("ready"), runtime_status)
        await self._wait_for_bridge_ready()

        avoid_rect = await self.conn.send_request(
            {"action": "get_avoid_rect_position"},
            timeout=8.0,
        )
        right = avoid_rect[2] if isinstance(avoid_rect[2], (int, float)) else 140
        top = avoid_rect[1] if isinstance(avoid_rect[1], (int, float)) else 140
        base_x = int(right) + 50
        base_y = int(top) + 50

        varnames = [f"live_soak_{int(time.time())}_{i}" for i in range(8)]
        self._created_varnames.extend(varnames)

        async def add_one(idx: int, varname: str):
            return await self.conn.send_request(
                {
                    "action": "add_object",
                    "position": [base_x + (idx * 28), base_y + (idx * 18)],
                    "obj_type": "button",
                    "args": [],
                    "varname": varname,
                },
                timeout=12.0,
            )

        add_results = await asyncio.gather(
            *(add_one(i, vn) for i, vn in enumerate(varnames)),
            return_exceptions=True,
        )
        for result in add_results:
            self.assertFalse(isinstance(result, Exception), result)
            if isinstance(result, dict):
                self.assertTrue(result.get("success", True), result)

        remove_results = await asyncio.gather(
            *(
                self.conn.send_request(
                    {"action": "remove_object", "varname": vn},
                    timeout=8.0,
                )
                for vn in varnames
            ),
            return_exceptions=True,
        )
        for result in remove_results:
            self.assertFalse(isinstance(result, Exception), result)
            if isinstance(result, dict):
                self.assertTrue(result.get("success", True), result)
        self._created_varnames = []

        metrics = self.conn.metrics_snapshot(include_events=False)
        self.assertGreaterEqual(metrics.get("total_requests", 0), 18)
        self.assertGreaterEqual(metrics["mutation_queue"]["max_depth_seen"], 1)


if __name__ == "__main__":
    unittest.main()
