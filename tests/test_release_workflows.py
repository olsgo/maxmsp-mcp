import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseWorkflowScriptTests(unittest.TestCase):
    def test_chaos_live_bridge_dry_run_schema(self):
        proc = subprocess.run(
            [
                str(PYTHON),
                "scripts/chaos_live_bridge.py",
                "--json",
                "--dry-run",
                "--no-artifacts",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("summary", payload)
        self.assertIn("aggregate_slo", payload)
        self.assertIn("scenario_results", payload)
        self.assertGreaterEqual(len(payload["scenario_results"]), 4)

    def test_maxpylang_check_extended_dry_run_schema(self):
        proc = subprocess.run(
            [
                str(PYTHON),
                "scripts/maxpylang_check_extended.py",
                "--json",
                "--dry-run",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("summary", payload)
        self.assertIn("metrics", payload["summary"])

    def test_check_live_release_rejects_skip_extended(self):
        proc = subprocess.run(
            [
                str(PYTHON),
                "scripts/check_live.py",
                "--profile",
                "release",
                "--skip-extended-check",
                "--json",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("release profile cannot be used with --skip-extended-check", payload["error"])

    def test_release_gate_fast_wires_check_live_quick_with_chaos(self):
        module = _load_module("release_gate_module", REPO_ROOT / "scripts" / "release_gate.py")
        captured_cmds: list[list[str]] = []

        def _fake_run_stage(name: str, cmd: list[str], timeout: int):
            _ = timeout
            captured_cmds.append(cmd)
            parsed = {"ok": True} if name == "check_live" else None
            return {
                "name": name,
                "command": cmd,
                "exit_code": 0,
                "duration_seconds": 0.01,
                "stdout": json.dumps(parsed) if parsed else "",
                "stderr": "",
                "ok": True,
                "parsed_json": parsed,
                "parse_error": None,
            }

        with patch.object(module, "_run_stage", side_effect=_fake_run_stage):
            with patch.object(sys, "argv", ["release_gate.py", "--profile", "fast", "--json"]):
                rc = module.main()

        self.assertEqual(rc, 0)
        self.assertEqual(len(captured_cmds), 2)
        live_cmd = captured_cmds[1]
        self.assertIn("scripts/check_live.py", live_cmd)
        self.assertIn("--profile", live_cmd)
        self.assertIn("quick", live_cmd)
        self.assertIn("--run-chaos", live_cmd)
        self.assertIn("--chaos-preset", live_cmd)


if __name__ == "__main__":
    unittest.main()
