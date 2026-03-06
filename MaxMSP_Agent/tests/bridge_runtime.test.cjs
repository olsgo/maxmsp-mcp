const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { buildBridgeConfig, envFlag, envInt, envString, protocol } = require("../bridge_runtime.cjs");
const protocolSpec = require("../../maxmsp_mcp/protocol_spec.json");

test("generated protocol stays in sync with protocol_spec.json", () => {
  assert.equal(protocol.protocol_version, protocolSpec.protocol_version);
  assert.equal(protocol.bridge_proto, protocolSpec.bridge_proto);
  assert.deepEqual(protocol.transports, protocolSpec.transports);
  assert.deepEqual(protocol.stream_kinds, protocolSpec.stream_kinds);
  assert.deepEqual(protocol.events, protocolSpec.events);
  for (const [key, value] of Object.entries(protocolSpec.actions)) {
    assert.equal(protocol.actions[key], value);
  }
  assert.deepEqual(protocol.error_codes, protocolSpec.error_codes);
  assert.deepEqual(
    protocol.transport_handoff_failure_markers,
    protocolSpec.transport_handoff_failure_markers
  );
});

test("buildBridgeConfig derives defaults from the shared protocol surface", () => {
  const config = buildBridgeConfig({});
  assert.equal(config.bridgeProto, protocol.bridge_proto);
  assert.equal(config.namespace, "/mcp");
  assert.equal(config.allowRemote, false);
  assert.equal(config.requireHandshakeAuth, true);
  assert.equal(config.transportInlineMaxChars, 24000);
  assert.equal(config.transportMaxTotalChars, 2000000);
  assert.equal(config.transportDictPrefix, "__maxmcp_transport");
  assert.equal(config.protocol.action_transport_dict_request, protocol.action_transport_dict_request);
  assert.equal(config.transportFileDir, undefined);
  assert.equal(config.transportRequestFileFallback, undefined);
});

test("buildBridgeConfig clamps invalid numeric values to safe minima", () => {
  const config = buildBridgeConfig({
    MAXMCP_TRANSPORT_INLINE_MAX_CHARS: "0",
    MAXMCP_TRANSPORT_MAX_TOTAL_CHARS: "10",
    MAXMCP_TRANSPORT_HEALTH_PROBE_INTERVAL_MS: "1",
    MAXMCP_TRANSPORT_FAILURE_COOLDOWN_MS: "2",
    MAXMCP_TRANSPORT_MAX_INFLIGHT: "0",
    MAXMCP_TRANSPORT_REQUEST_RETRY_DELAY_MS: "-10",
  });
  assert.equal(config.transportInlineMaxChars, 1024);
  assert.equal(config.transportMaxTotalChars, 1024);
  assert.equal(config.transportHealthProbeIntervalMs, 250);
  assert.equal(config.transportFailureCooldownMs, 250);
  assert.equal(config.transportMaxInflight, 1);
  assert.equal(config.transportRequestRetryDelayMs, 0);
});

test("bridge env helpers normalize booleans, integers, and strings", () => {
  assert.equal(envFlag("YES"), true);
  assert.equal(envFlag("", true), true);
  assert.equal(envFlag(undefined, false), false);
  assert.equal(envInt("12", 5, 1), 12);
  assert.equal(envInt("bad", 5, 1), 5);
  assert.equal(envInt("-5", 5, 0), 0);
  assert.equal(envString("  value  ", "fallback"), "value");
  assert.equal(envString("   ", "fallback"), "fallback");
});

test("node bridge source is dict-only on the request handoff path", () => {
  const source = fs.readFileSync(path.join(__dirname, "../max_mcp_node.js"), "utf8");
  assert.equal(source.includes("forward_file_request"), false);
  assert.equal(source.includes("TRANSPORT_REQUEST_FILE_FALLBACK"), false);
  assert.equal(source.includes("sweep_stale_transport_files"), false);
  assert.equal(source.includes("file_fallback_ratio"), false);
  assert.equal(source.includes("file_fallback_attempts"), false);
});

test("max js source rejects file transport without payload-file helpers", () => {
  const source = fs.readFileSync(path.join(__dirname, "../max_mcp.js"), "utf8");
  assert.equal(source.includes("_handle_transport_file_request_control"), false);
  assert.equal(source.includes("_read_transport_file_payload"), false);
  assert.equal(source.includes("_validate_transport_file_reference"), false);
  assert.match(source, /File request transport is disabled\. Use dict_ref transport\./);
});
