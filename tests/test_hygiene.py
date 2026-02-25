import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from server import ERROR_VALIDATION, MaxHygieneManager, MaxMSPConnection, MaxRuntimeManager


class HygieneManagerTests(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(self, root: Path) -> tuple[MaxMSPConnection, MaxRuntimeManager, MaxHygieneManager]:
        conn = MaxMSPConnection("http://127.0.0.1", "5002")
        runtime = MaxRuntimeManager(conn)
        runtime.sessions_root = root / "sessions"
        runtime.sessions_root.mkdir(parents=True, exist_ok=True)
        runtime.session_id = "current"
        runtime.session_dir = runtime.sessions_root / runtime.session_id
        runtime.session_dir.mkdir(parents=True, exist_ok=True)
        runtime.session_active_patch = runtime.session_dir / "active.maxpat"
        runtime.session_scratch_patch = runtime.session_dir / "scratch.maxpat"
        runtime.checkpoints_file = runtime.session_dir / "checkpoints.json"
        runtime._ensure_session_patches_sync()
        hygiene = MaxHygieneManager(runtime, conn)
        runtime.hygiene_manager = hygiene
        return conn, runtime, hygiene

    async def test_inventory_classifies_bridge_owner_and_stale_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, _runtime, hygiene = self._make_runtime(Path(tmp))
            conn.sio.connected = True
            hygiene.stale_seconds = 1800
            hygiene._read_process_table_sync = lambda: [
                {
                    "pid": 101,
                    "ppid": 1,
                    "elapsed_seconds": 4000,
                    "cpu_pct": 0.1,
                    "rss_mb": 120.0,
                    "command": "/Applications/Max.app/Contents/MacOS/Max",
                },
                {
                    "pid": 102,
                    "ppid": 1,
                    "elapsed_seconds": 5000,
                    "cpu_pct": 0.1,
                    "rss_mb": 110.0,
                    "command": "/Applications/Max.app/Contents/MacOS/Max",
                },
            ]
            hygiene._discover_bridge_owner_pid_sync = lambda *_args, **_kwargs: 102
            hygiene._scan_open_documents_sync = lambda: {"available": True, "reason": None, "documents": []}
            hygiene._discover_managed_session_dirs_sync = lambda: []

            inventory = await hygiene.list_system_sessions(include_windows=True, include_runtime_state=True)
            by_pid = {row["pid"]: row for row in inventory["max_processes"]}
            self.assertTrue(by_pid[101]["is_stale"])
            self.assertFalse(by_pid[102]["is_stale"])
            self.assertEqual(by_pid[102]["classified_as"], "managed_bridge_owner")
            self.assertTrue(inventory["open_patch_windows"]["available"])

    async def test_cleanup_removes_stale_session_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, runtime, hygiene = self._make_runtime(Path(tmp))
            conn.sio.connected = False
            hygiene.stale_seconds = 1
            hygiene.keep_recent_sessions = 0
            hygiene._read_process_table_sync = lambda: []
            hygiene._scan_open_documents_sync = lambda: {
                "available": False,
                "reason": "disabled",
                "documents": [],
            }

            stale_ids = ["stale_a", "stale_b", "stale_c"]
            for sid in stale_ids:
                d = runtime.sessions_root / sid
                d.mkdir(parents=True, exist_ok=True)
                (d / "active.maxpat").write_text("{}")
                old = time.time() - 3600
                for p in [d, d / "active.maxpat"]:
                    try:
                        os.utime(p, (old, old))
                    except Exception:
                        pass

            result = await hygiene.cleanup_hygiene(
                mode="aggressive",
                include_processes=False,
                include_session_dirs=True,
                dry_run=False,
                reason="unit_test",
            )
            self.assertTrue(result["success"])
            self.assertGreaterEqual(result["summary"]["sessions_deleted"], 3)
            for sid in stale_ids:
                self.assertFalse((runtime.sessions_root / sid).exists())
            self.assertTrue((runtime.sessions_root / runtime.session_id).exists())

    async def test_close_stale_sessions_supports_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            _conn, _runtime, hygiene = self._make_runtime(Path(tmp))
            hygiene.list_system_sessions = lambda **_kwargs: asyncio.sleep(
                0,
                result={
                    "max_processes": [
                        {"pid": 201, "classified_as": "non_managed_max", "is_stale": True},
                        {"pid": 202, "classified_as": "non_managed_max", "is_stale": False},
                    ],
                    "summary": {"max_process_count": 2},
                },
            )
            result = await hygiene.close_max_system_sessions(target="stale", dry_run=True)
            self.assertTrue(result["success"])
            self.assertEqual(result["summary"]["requested"], 1)
            self.assertTrue(result["actions_taken"][0]["dry_run"])

    async def test_cleanup_rejects_invalid_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            _conn, _runtime, hygiene = self._make_runtime(Path(tmp))
            result = await hygiene.cleanup_hygiene(mode="invalid_mode")
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], ERROR_VALIDATION)


if __name__ == "__main__":
    unittest.main()
