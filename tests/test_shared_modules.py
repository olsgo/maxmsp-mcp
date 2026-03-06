import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from maxmsp_mcp import protocol
from maxmsp_mcp.config import load_settings
from maxmsp_mcp.json_utils import (
    canonical_json,
    compact_json_size,
    parse_json_object_text,
    read_json_file,
    read_json_object_file,
    write_json_file,
)
from maxmsp_mcp.object_specs import (
    convert_string_args,
    normalize_add_object_spec,
    normalize_avoid_rect_payload,
    validate_add_object_payload,
)
from maxmsp_mcp.process_utils import run_command_json_object
from maxmsp_mcp.qa_utils import collect_patch_audit
from maxmsp_mcp.release_utils import render_text_for_diff
from maxmsp_mcp.shared_daemon import (
    SHARED_DAEMON_MODE,
    build_sse_url,
    choose_daemon_port,
    normalize_multi_client_mode,
    parse_shared_daemon_payload,
)
from maxmsp_mcp.topology import (
    Topology,
    TopologyError,
    clone_json_data,
    load_patch_topology,
    normalize_import_topology,
    patch_payload_from_template,
    topology_hash,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class SharedProtocolTests(unittest.TestCase):
    def test_generated_protocol_matches_spec(self):
        spec = read_json_file(REPO_ROOT / "maxmsp_mcp" / "protocol_spec.json")
        self.assertEqual(protocol.PROTOCOL_VERSION, spec["protocol_version"])
        self.assertEqual(protocol.DEFAULT_BRIDGE_PROTO, spec["bridge_proto"])
        self.assertEqual(protocol.TRANSPORTS, spec["transports"])
        self.assertEqual(protocol.STREAM_KINDS, spec["stream_kinds"])
        self.assertEqual(protocol.EVENTS, spec["events"])
        self.assertEqual(protocol.ACTIONS, spec["actions"])
        self.assertEqual(protocol.ERROR_CODES, spec["error_codes"])
        self.assertEqual(
            protocol.TRANSPORT_HANDOFF_FAILURE_MARKERS,
            tuple(spec["transport_handoff_failure_markers"]),
        )

    def test_settings_default_bridge_proto_tracks_generated_protocol(self):
        settings = load_settings(REPO_ROOT)
        self.assertEqual(settings.runtime.bridge_proto, protocol.DEFAULT_BRIDGE_PROTO)


class SharedDaemonTests(unittest.TestCase):
    def test_normalize_multi_client_mode_defaults_to_shared_daemon(self):
        self.assertEqual(normalize_multi_client_mode(""), SHARED_DAEMON_MODE)
        self.assertEqual(normalize_multi_client_mode("bogus"), SHARED_DAEMON_MODE)

    def test_build_sse_url_normalizes_path(self):
        self.assertEqual(build_sse_url("127.0.0.1", 8765), "http://127.0.0.1:8765/sse")
        self.assertEqual(build_sse_url("127.0.0.1", 8765, "events"), "http://127.0.0.1:8765/events")

    def test_parse_shared_daemon_payload_requires_live_daemon_role(self):
        payload = {
            "pid": 123,
            "server_role": "daemon",
            "share_url": "http://127.0.0.1:8765/sse",
            "share_host": "127.0.0.1",
            "share_port": 8765,
        }
        parsed = parse_shared_daemon_payload(payload, pid_alive=lambda pid: pid == 123)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["share_url"], payload["share_url"])
        self.assertIsNone(parse_shared_daemon_payload(payload, pid_alive=lambda _pid: False))

    def test_choose_daemon_port_returns_bindable_port(self):
        port = choose_daemon_port("127.0.0.1", 0)
        self.assertGreater(port, 0)


class SharedJsonUtilsTests(unittest.TestCase):
    def test_write_and_read_json_file_round_trip(self):
        payload = {"b": 2, "a": {"nested": True}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.json"
            write_json_file(path, payload, sort_keys=True)
            self.assertEqual(read_json_file(path), payload)
            text = path.read_text()
            self.assertLess(text.find('"a"'), text.find('"b"'))

    def test_parse_json_object_text_rejects_non_object(self):
        parsed, error = parse_json_object_text("[1, 2, 3]")
        self.assertIsNone(parsed)
        self.assertEqual(error, "JSON payload is not an object")

    def test_canonical_json_stabilizes_key_order(self):
        left = canonical_json({"b": 1, "a": 2})
        right = canonical_json({"a": 2, "b": 1})
        self.assertEqual(left, right)

    def test_compact_json_size_matches_compact_serialization(self):
        payload = {"action": "ping", "payload": {"message": "hello"}}
        self.assertEqual(compact_json_size(payload), len('{"action":"ping","payload":{"message":"hello"}}'))

    def test_read_json_object_file_returns_default_for_non_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.json"
            path.write_text('["not", "an", "object"]', encoding="utf-8")
            self.assertEqual(read_json_object_file(path), {})


class SharedProcessUtilsTests(unittest.TestCase):
    def test_run_command_json_object_parses_stdout(self):
        with patch("maxmsp_mcp.process_utils.run_command") as mock_run:
            mock_run.return_value = Mock(
                stdout='{"ok": true, "message": "ready"}',
                stderr="",
                returncode=0,
            )
            proc, payload, parse_error = run_command_json_object(["echo", "ignored"])

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(payload, {"ok": True, "message": "ready"})
        self.assertIsNone(parse_error)


class SharedObjectSpecsTests(unittest.TestCase):
    def test_normalize_add_object_spec_rejects_legacy_newobj(self):
        obj_type, args, rewrite, error = normalize_add_object_spec("newobj", ["prepend", "set"])
        self.assertEqual(obj_type, "newobj")
        self.assertEqual(args, ["prepend", "set"])
        self.assertIsNone(rewrite)
        self.assertIsNotNone(error)
        self.assertEqual(error["error"]["code"], "VALIDATION_ERROR")

    def test_convert_string_args_preserves_numeric_intent(self):
        self.assertEqual(convert_string_args(["25.", "127", "@embed", 1]), [25.0, 127, "@embed", 1])

    def test_normalize_avoid_rect_payload_rejects_invalid_shape(self):
        rect, valid = normalize_avoid_rect_payload({"bad": "shape"})
        self.assertEqual(rect, [0.0, 0.0, 0.0, 0.0])
        self.assertFalse(valid)

    def test_validate_add_object_payload_enforces_trigger_ack(self):
        error = validate_add_object_payload(
            obj_type="trigger",
            args=["b", "f"],
            int_mode=False,
            extend=False,
            use_live_dial=False,
            trigger_rtl=False,
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("RIGHT-TO-LEFT", error["error"]["message"])


class SharedReleaseUtilsTests(unittest.TestCase):
    def test_render_text_for_diff_pretty_prints_json_without_maxdiff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "patch.maxpat"
            path.write_text('{"b":2,"a":{"nested":true}}', encoding="utf-8")
            text, backend, warnings = render_text_for_diff(
                path,
                prefer_maxdiff=False,
                maxdevtools_root=Path(tmp),
            )

        self.assertEqual(backend, "internal")
        self.assertEqual(warnings, [])
        self.assertIn('"a": {', text)
        self.assertIn('"nested": true', text)


class SharedQaUtilsTests(unittest.TestCase):
    def test_collect_patch_audit_flags_release_issues_and_signal_warning(self):
        topology = {
            "boxes": [
                {
                    "box": {
                        "maxclass": "newobj",
                        "varname": "dbg",
                        "boxtext": "print debug",
                        "patching_rect": [10.5, 20, 80, 20],
                    }
                },
                {
                    "box": {
                        "maxclass": "comment",
                        "text": "TODO clean this up",
                        "patching_rect": [20, 40, 80, 20],
                    }
                },
            ],
            "lines": [],
        }
        audit = collect_patch_audit(
            topology,
            signal_safety={
                "safe": False,
                "warnings": [
                    {
                        "type": "UNSAFE_FEEDBACK",
                        "message": "Feedback loop will run away",
                    }
                ],
            },
        )

        self.assertFalse(audit["strict_passed"])
        self.assertGreater(audit["summary"]["critical_findings"], 0)
        self.assertTrue(any(item["id"] == "no_print_objects" for item in audit["findings"]))
        self.assertTrue(any(item["id"] == "no_todo_comments" for item in audit["findings"]))
        self.assertTrue(any(item["id"] == "signal_warning_1" for item in audit["findings"]))


class SharedTopologyTests(unittest.TestCase):
    def test_clone_json_data_alias_deep_copies_values(self):
        original = {"nested": [{"x": 1}]}
        cloned = clone_json_data(original)
        cloned["nested"][0]["x"] = 2
        self.assertEqual(original["nested"][0]["x"], 1)

    def test_patch_payload_from_template_injects_normalized_topology(self):
        template = {"patcher": {"boxes": [{"stale": True}], "lines": [{"stale": True}]}}
        topology = {
            "boxes": [{"box": {"varname": "obj-1"}}],
            "lines": [{"patchline": {"source": ["obj-1", 0], "destination": ["obj-2", 0]}}],
        }
        payload = patch_payload_from_template(template, topology)
        self.assertEqual(payload["patcher"]["boxes"], topology["boxes"])
        self.assertEqual(payload["patcher"]["lines"], topology["lines"])
        digest, object_count, connection_count = topology_hash(topology)
        self.assertEqual(len(digest), 64)
        self.assertEqual(object_count, 1)
        self.assertEqual(connection_count, 1)

    def test_topology_hash_is_stable_across_input_ordering(self):
        topology_a = {
            "boxes": [
                {
                    "box": {
                        "varname": "b",
                        "maxclass": "message",
                        "attributes": {"z": 2, "a": [3, {"k": "v"}]},
                    }
                },
                {
                    "box": {
                        "varname": "a",
                        "maxclass": "comment",
                        "attributes": {"nested": {"y": 1, "x": 0}},
                    }
                },
            ],
            "lines": [
                {"patchline": {"source": ["b", 0], "destination": ["a", 0]}},
                {"patchline": {"source": ["a", 0], "destination": ["b", 0]}},
            ],
        }
        topology_b = {
            "boxes": [
                {
                    "box": {
                        "maxclass": "comment",
                        "attributes": {"nested": {"x": 0, "y": 1}},
                        "varname": "a",
                    }
                },
                {
                    "box": {
                        "attributes": {"a": [3, {"k": "v"}], "z": 2},
                        "maxclass": "message",
                        "varname": "b",
                    }
                },
            ],
            "lines": [
                {"patchline": {"destination": ["b", 0], "source": ["a", 0]}},
                {"patchline": {"destination": ["a", 0], "source": ["b", 0]}},
            ],
        }

        digest_a = topology_hash(topology_a)
        digest_b = topology_hash(topology_b)
        self.assertEqual(digest_a, digest_b)
        self.assertEqual(
            Topology.from_payload(topology_a).canonical_payload(),
            Topology.from_payload(topology_b).canonical_payload(),
        )

    def test_load_patch_topology_extracts_patcher_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "patch.maxpat"
            path.write_text(
                '{"patcher":{"boxes":[{"box":{"maxclass":"newobj","varname":"osc1"}}],"lines":[]}}',
                encoding="utf-8",
            )

            loaded = load_patch_topology(path)

        self.assertEqual(loaded["format"], "maxpat_patcher")
        self.assertEqual(loaded["object_count"], 1)
        self.assertEqual(loaded["connection_count"], 0)
        self.assertEqual(loaded["topology"]["boxes"][0]["box"]["varname"], "osc1")

    def test_normalize_import_topology_generates_varnames_and_remaps_ids(self):
        normalized = normalize_import_topology(
            {
                "boxes": [
                    {"box": {"id": "obj-1", "maxclass": "newobj"}},
                    {"box": {"id": "obj-2", "maxclass": "newobj"}},
                ],
                "lines": [
                    {"patchline": {"source": ["obj-1", 0], "destination": ["obj-2", 0]}},
                ],
            }
        )

        self.assertEqual(normalized["generated_varnames"], 2)
        self.assertGreaterEqual(normalized["line_ref_id_mappings"], 2)
        self.assertEqual(len(normalized["topology"]["lines"]), 1)

    def test_normalize_import_topology_rejects_duplicate_source_varnames(self):
        with self.assertRaises(TopologyError) as ctx:
            normalize_import_topology(
                {
                    "boxes": [
                        {"box": {"maxclass": "newobj", "varname": "dup"}},
                        {"box": {"maxclass": "newobj", "varname": "dup"}},
                    ],
                    "lines": [],
                }
            )

        self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
