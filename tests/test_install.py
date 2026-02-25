import tempfile
import unittest
from pathlib import Path
import re

import install


class InstallConfigTests(unittest.TestCase):
    def test_build_common_env_includes_hygiene_defaults(self):
        env = install.build_common_env("/tmp/maxmsp-mcp", "tok")
        self.assertEqual(env["MAXMCP_HYGIENE_AUTO_CLEANUP"], "1")
        self.assertEqual(env["MAXMCP_HYGIENE_SCOPE"], "all_max_instances")
        self.assertEqual(env["MAXMCP_HYGIENE_MODE"], "aggressive")
        self.assertEqual(env["MAXMCP_HYGIENE_STALE_SECONDS"], "1800")
        self.assertEqual(env["MAXMCP_HYGIENE_STARTUP_SWEEP"], "1")
        self.assertEqual(env["MAXMCP_HYGIENE_MAX_KILLS_PER_SWEEP"], "50")

    def test_resolve_auth_token_preserves_existing(self):
        self.assertEqual(install.resolve_auth_token("existing-token"), "existing-token")

    def test_resolve_auth_token_generates_when_missing(self):
        token = install.resolve_auth_token("")
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 24)

    def test_extract_codex_auth_token_reads_env_table_value(self):
        text = """
[mcp_servers.maxmsp.env]
MAXMCP_AUTH_TOKEN = "keep-me"
MAXMCP_MANAGED_MODE = "1"
"""
        self.assertEqual(install.extract_codex_auth_token(text), "keep-me")

    def test_install_codex_config_preserves_existing_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(
                """
[mcp_servers.maxmsp]
command = "uv"
args = ["run", "server.py"]

[mcp_servers.maxmsp.env]
MAXMCP_AUTH_TOKEN = "persist-token"
"""
            )

            install.install_codex_config(config, str(root))
            updated = config.read_text()
            self.assertEqual(install.extract_codex_auth_token(updated), "persist-token")
            auth_token_lines = re.findall(
                r'^\s*MAXMCP_AUTH_TOKEN\s*=\s*"[^"]+"\s*$',
                updated,
                re.MULTILINE,
            )
            self.assertEqual(len(auth_token_lines), 1)
            self.assertIn("MAXMCP_AUTH_TOKEN_FILE", updated)
            self.assertIn("[mcp_servers.maxmsp]", updated)
            self.assertIn("[mcp_servers.maxmsp.env]", updated)

    def test_install_codex_config_generates_token_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text("")

            install.install_codex_config(config, str(root))
            updated = config.read_text()
            token = install.extract_codex_auth_token(updated)
            self.assertTrue(token)
            self.assertGreaterEqual(len(token), 24)


if __name__ == "__main__":
    unittest.main()
