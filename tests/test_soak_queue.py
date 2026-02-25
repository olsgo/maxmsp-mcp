import asyncio
import unittest

from server import ERROR_OVERLOADED, MaxMCPError, MaxMSPConnection


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


class QueueSoakTests(unittest.IsolatedAsyncioTestCase):
    async def test_high_concurrency_mutations_no_deadlock(self):
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        conn.capabilities = {"supported_actions": ["add_object"]}
        conn.mutation_max_inflight = 3
        conn.mutation_max_queue = 12
        conn.mutation_queue_wait_timeout_seconds = 1.0

        async def handler(_event, payload, _namespace):
            await asyncio.sleep(0.01)
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

        async def submit(i: int):
            try:
                return await conn.send_request(
                    {
                        "action": "add_object",
                        "position": [10 + i, 10 + i],
                        "obj_type": "button",
                        "varname": f"soak_{i}",
                        "args": [],
                    },
                    timeout=3.0,
                )
            except MaxMCPError as e:
                if e.code == ERROR_OVERLOADED:
                    return {"overloaded": True}
                raise

        tasks = [asyncio.create_task(submit(i)) for i in range(60)]
        results = await asyncio.gather(*tasks)
        overloaded = [r for r in results if isinstance(r, dict) and r.get("overloaded")]
        succeeded = [r for r in results if isinstance(r, dict) and r.get("ok")]

        self.assertEqual(len(results), 60)
        self.assertGreater(len(succeeded), 0)
        self.assertGreater(len(overloaded), 0)
        self.assertEqual(conn._inflight_mutation_requests, 0)
        self.assertEqual(conn._queued_mutation_requests, 0)

        metrics = conn.metrics_snapshot()
        self.assertGreaterEqual(metrics["total_requests"], len(succeeded))
        self.assertGreater(metrics["mutation_queue"]["max_depth_seen"], 0)


if __name__ == "__main__":
    unittest.main()
