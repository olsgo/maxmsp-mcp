const protocol = require("./protocol.generated.cjs");

function envFlag(rawValue, defaultValue = false) {
  if (rawValue === undefined || rawValue === null || String(rawValue).trim() === "") {
    return defaultValue;
  }
  return ["1", "true", "yes", "on"].includes(String(rawValue).trim().toLowerCase());
}

function envInt(rawValue, fallbackValue, minimumValue) {
  const parsed = parseInt(String(rawValue ?? ""), 10);
  let value = Number.isFinite(parsed) ? parsed : fallbackValue;
  if (Number.isFinite(minimumValue)) {
    value = Math.max(minimumValue, value);
  }
  return value;
}

function envString(rawValue, fallbackValue) {
  const value = String(rawValue ?? "").trim();
  return value || fallbackValue;
}

function buildBridgeConfig(env = process.env) {
  const transportInlineMaxChars = envInt(
    env.MAXMCP_TRANSPORT_INLINE_MAX_CHARS,
    24000,
    1024
  );
  const transportHealthProbeIntervalMs = envInt(
    env.MAXMCP_TRANSPORT_HEALTH_PROBE_INTERVAL_MS,
    1500,
    250
  );

  return {
    namespace: "/mcp",
    bridgeProto: envString(env.MAXMCP_BRIDGE_PROTO, protocol.bridge_proto),
    allowRemote: envFlag(env.MAXMCP_ALLOW_REMOTE, false),
    authTokenFile: envString(env.MAXMCP_AUTH_TOKEN_FILE, ""),
    requireHandshakeAuth: envFlag(env.MAXMCP_REQUIRE_HANDSHAKE_AUTH, true),
    transportInlineMaxChars,
    transportMaxTotalChars: envInt(
      env.MAXMCP_TRANSPORT_MAX_TOTAL_CHARS,
      2000000,
      transportInlineMaxChars
    ),
    transportDictTtlMs: envInt(env.MAXMCP_TRANSPORT_DICT_TTL_MS, 45000, 1000),
    transportMaxInflight: envInt(env.MAXMCP_TRANSPORT_MAX_INFLIGHT, 48, 1),
    transportInflightTtlMs: envInt(env.MAXMCP_TRANSPORT_INFLIGHT_TTL_MS, 45000, 1000),
    transportInflightSweepMs: envInt(env.MAXMCP_TRANSPORT_INFLIGHT_SWEEP_MS, 2000, 250),
    transportHealthProbeIntervalMs,
    transportFailureCooldownMs: envInt(
      env.MAXMCP_TRANSPORT_FAILURE_COOLDOWN_MS,
      5000,
      transportHealthProbeIntervalMs
    ),
    transportFailureThreshold: envInt(env.MAXMCP_TRANSPORT_FAILURE_THRESHOLD, 3, 1),
    transportRequestRetryAttempts: envInt(
      env.MAXMCP_TRANSPORT_REQUEST_RETRY_ATTEMPTS,
      2,
      1
    ),
    transportRequestRetryDelayMs: envInt(env.MAXMCP_TRANSPORT_REQUEST_RETRY_DELAY_MS, 120, 0),
    transportDictPrefix: envString(env.MAXMCP_TRANSPORT_DICT_PREFIX, "__maxmcp_transport"),
    projectRootDir: envString(env.MAXMCP_PROJECT_ROOT_DIR, process.cwd()),
    protocol,
  };
}

module.exports = {
  buildBridgeConfig,
  envFlag,
  envInt,
  envString,
  protocol,
};
