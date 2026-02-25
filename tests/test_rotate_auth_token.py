import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _load_rotate_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "rotate_auth_token.py"
    spec = importlib.util.spec_from_file_location("rotate_auth_token", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class RotateAuthTokenTests(unittest.TestCase):
    def test_write_token_file_creates_secure_file(self):
        module = _load_rotate_module()
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "auth_token"
            module._write_token_file(token_path, "abc123")
            self.assertTrue(token_path.exists())
            self.assertEqual(token_path.read_text(encoding="utf-8").strip(), "abc123")

    def test_update_json_client_create_entry(self):
        module = _load_rotate_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "cursor.json"
            result = module._update_json_client(
                client="cursor",
                config_path=cfg,
                current_dir=str(root),
                token="token-value",
                token_file=str(root / "token_file"),
                create_entry=True,
            )
            self.assertTrue(result["updated"])
            payload = json.loads(cfg.read_text(encoding="utf-8"))
            env = payload["mcpServers"]["MaxMSPMCP"]["env"]
            self.assertEqual(env["MAXMCP_AUTH_TOKEN"], "token-value")
            self.assertIn("MAXMCP_AUTH_TOKEN_FILE", env)


if __name__ == "__main__":
    unittest.main()
