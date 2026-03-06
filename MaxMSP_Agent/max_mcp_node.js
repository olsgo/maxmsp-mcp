autowatch = 1;

const Max = require("max-api");
const { Server } = require("socket.io");
const crypto = require("crypto");
const fs = require("fs");
const { buildBridgeConfig } = require("./bridge_runtime.cjs");

const BRIDGE_RUNTIME = buildBridgeConfig(process.env);
const PROTOCOL = BRIDGE_RUNTIME.protocol;

// Configuration
var PORT = 5002;
const NAMESPACE = BRIDGE_RUNTIME.namespace;
const BRIDGE_PROTO = BRIDGE_RUNTIME.bridgeProto;
const NODE_BRIDGE_BUILD_ID = String(
  process.env.MAXMCP_NODE_BRIDGE_BUILD_ID || "max_mcp_node_js_20260302_02"
);
const TRANSPORT_DICT_REF = PROTOCOL.transport_dict_ref;
const STREAM_KIND_REQUEST = PROTOCOL.stream_kind_request;
const STREAM_KIND_HELLO = PROTOCOL.stream_kind_hello;
const MAXMCP_ALLOW_REMOTE = BRIDGE_RUNTIME.allowRemote;
const MAXMCP_AUTH_TOKEN_FILE = BRIDGE_RUNTIME.authTokenFile;
const MAXMCP_REQUIRE_HANDSHAKE_AUTH = BRIDGE_RUNTIME.requireHandshakeAuth;
const TRANSPORT_INLINE_MAX_CHARS = BRIDGE_RUNTIME.transportInlineMaxChars;
const TRANSPORT_MAX_TOTAL_CHARS = BRIDGE_RUNTIME.transportMaxTotalChars;
const TRANSPORT_DICT_TTL_MS = BRIDGE_RUNTIME.transportDictTtlMs;
const TRANSPORT_MAX_INFLIGHT = BRIDGE_RUNTIME.transportMaxInflight;
const TRANSPORT_INFLIGHT_TTL_MS = BRIDGE_RUNTIME.transportInflightTtlMs;
const TRANSPORT_INFLIGHT_SWEEP_MS = BRIDGE_RUNTIME.transportInflightSweepMs;
const TRANSPORT_HEALTH_PROBE_INTERVAL_MS = BRIDGE_RUNTIME.transportHealthProbeIntervalMs;
const TRANSPORT_FAILURE_COOLDOWN_MS = BRIDGE_RUNTIME.transportFailureCooldownMs;
const TRANSPORT_FAILURE_THRESHOLD = BRIDGE_RUNTIME.transportFailureThreshold;
const TRANSPORT_REQUEST_RETRY_ATTEMPTS = BRIDGE_RUNTIME.transportRequestRetryAttempts;
const TRANSPORT_REQUEST_RETRY_DELAY_MS = BRIDGE_RUNTIME.transportRequestRetryDelayMs;
const TRANSPORT_DICT_PREFIX = BRIDGE_RUNTIME.transportDictPrefix;

const ACTION_TRANSPORT_DICT_REQUEST = PROTOCOL.action_transport_dict_request;
const ACTION_TRANSPORT_DICT_RESPONSE = PROTOCOL.action_transport_dict_response;
const BRIDGE_NODE_HELLO_EVENT = PROTOCOL.bridge_node_hello_event;
const ERROR_CODES = PROTOCOL.error_codes;

const transportHealthState = {
  dict_api_available: false,
  dict_ready: false,
  last_probe_ok: false,
  last_probe_at_ms: 0,
  last_error: "",
  last_probe_id: "",
  consecutive_failures: 0,
  circuit_open_until_ms: 0,
  handoff_stats: {
    dict_attempts: 0,
    dict_successes: 0,
    dict_failures: 0,
    last_handoff_mode: ""
  }
};
const inflightRequests = new Map();

function resolve_auth_token() {
  const fromEnv = String(process.env.MAXMCP_AUTH_TOKEN || "").trim();
  if (fromEnv) {
    return fromEnv;
  }
  if (!MAXMCP_AUTH_TOKEN_FILE) {
    return "";
  }
  try {
    const fromFile = fs.readFileSync(MAXMCP_AUTH_TOKEN_FILE, "utf8").trim();
    return fromFile;
  } catch (e) {
    return "";
  }
}

const MAXMCP_AUTH_TOKEN = resolve_auth_token();

function build_server_options() {
  if (MAXMCP_ALLOW_REMOTE) {
    return { cors: { origin: "*" } };
  }
  return {
    cors: {
      origin: [/^https?:\/\/localhost(:\d+)?$/, /^https?:\/\/127\.0\.0\.1(:\d+)?$/]
    }
  };
}

function is_local_address(addr) {
  if (!addr) return false;
  return (
    addr === "127.0.0.1" ||
    addr === "::1" ||
    addr === "::ffff:127.0.0.1" ||
    addr.indexOf("localhost") !== -1
  );
}

function extract_auth_token(data) {
  if (!data || typeof data !== "object") return "";
  if (typeof data.auth_token === "string") return data.auth_token;
  if (data.auth && typeof data.auth === "object" && typeof data.auth.token === "string") {
    return data.auth.token;
  }
  return "";
}

function extract_handshake_token(socket) {
  if (!socket || !socket.handshake) return "";
  const auth = socket.handshake.auth;
  if (auth && typeof auth === "object" && typeof auth.token === "string") {
    return auth.token;
  }
  const headers = socket.handshake.headers || {};
  if (typeof headers["x-maxmcp-token"] === "string") {
    return headers["x-maxmcp-token"];
  }
  if (typeof headers.authorization === "string") {
    const authz = headers.authorization;
    if (authz.toLowerCase().indexOf("bearer ") === 0) {
      return authz.slice(7).trim();
    }
  }
  return "";
}

function secure_equals(expected, provided) {
  if (typeof expected !== "string" || typeof provided !== "string") {
    return false;
  }
  const expectedBuf = Buffer.from(expected, "utf8");
  const providedBuf = Buffer.from(provided, "utf8");
  if (expectedBuf.length !== providedBuf.length) {
    return false;
  }
  try {
    return crypto.timingSafeEqual(expectedBuf, providedBuf);
  } catch (e) {
    return false;
  }
}

function is_authorized_token(token) {
  if (!MAXMCP_AUTH_TOKEN) {
    return true;
  }
  if (!token) {
    return false;
  }
  return secure_equals(MAXMCP_AUTH_TOKEN, String(token));
}

function build_unauthorized_response(data, message) {
  const reqId = data && data.request_id ? data.request_id : null;
  return {
    protocol_version: "2.0",
    bridge_proto: BRIDGE_PROTO,
    request_id: reqId,
    state: "failed",
    timestamp_ms: Date.now(),
    error: {
      code: ERROR_CODES.unauthorized,
      message: message || "Authentication token missing or invalid.",
      recoverable: true,
      details: {}
    }
  };
}

function build_failed_response(data, code, message, recoverable, details) {
  const reqId = data && data.request_id ? data.request_id : null;
  const normalizedDetails = (details && typeof details === "object") ? { ...details } : {};
  if (normalizedDetails.node_bridge_build_id === undefined) {
    normalizedDetails.node_bridge_build_id = NODE_BRIDGE_BUILD_ID;
  }
  if (normalizedDetails.dict_api_available === undefined) {
    normalizedDetails.dict_api_available = supports_dict_api();
  }
  if (normalizedDetails.transport_mode === undefined) {
    normalizedDetails.transport_mode = TRANSPORT_DICT_REF;
  }
  if (normalizedDetails.stream_kind === undefined) {
    normalizedDetails.stream_kind = STREAM_KIND_REQUEST;
  }
  return {
    protocol_version: "2.0",
    bridge_proto: BRIDGE_PROTO,
    request_id: reqId,
    state: "failed",
    timestamp_ms: Date.now(),
    error: {
      code: code || ERROR_CODES.internal,
      message: message || "Bridge transport failure.",
      recoverable: recoverable !== false,
      details: normalizedDetails
    }
  };
}

function reject_if_unauthorized(socket, data) {
  if (!MAXMCP_AUTH_TOKEN) {
    return false;
  }
  if (socket && socket.data && socket.data.handshakeAuthorized) {
    return false;
  }
  const provided = extract_auth_token(data);
  if (is_authorized_token(provided)) {
    return false;
  }
  socket.emit("response", build_unauthorized_response(data));
  return true;
}

function normalize_outbound_request(data) {
  const normalized = (data && typeof data === "object") ? { ...data } : {};
  normalized.bridge_proto = BRIDGE_PROTO;
  return normalized;
}

function build_transport_id(requestId) {
  return [
    requestId || "no-request-id",
    Date.now(),
    Math.floor(Math.random() * 1000000)
  ].join(":");
}

function supports_dict_api() {
  return typeof Max.setDict === "function" && typeof Max.getDict === "function";
}

function now_ms() {
  return Date.now();
}

function normalize_reason(reason) {
  return String(reason || "").slice(0, 512);
}

function describe_error(err) {
  if (err === undefined || err === null) {
    return "unknown";
  }
  if (typeof err === "string") {
    return err;
  }
  const parts = [];
  if (err.name) {
    parts.push(String(err.name));
  }
  if (err.message) {
    parts.push(String(err.message));
  }
  if (err.code !== undefined && err.code !== null) {
    parts.push("code=" + String(err.code));
  }
  if (!parts.length) {
    try {
      return JSON.stringify(err);
    } catch (_stringify_error) {
      return String(err);
    }
  }
  let rendered = parts.join(" ");
  if (err.stack && typeof err.stack === "string") {
    const stackHead = err.stack.split("\n")[0];
    if (stackHead && stackHead !== rendered) {
      rendered += " stack=" + stackHead;
    }
  }
  return rendered;
}

function sleep_ms(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function transport_health_snapshot() {
  const now = now_ms();
  if (transportHealthState.circuit_open_until_ms > 0 && now >= transportHealthState.circuit_open_until_ms) {
    transportHealthState.circuit_open_until_ms = 0;
  }
  const handoff = transport_handoff_snapshot();
  return {
    dict_api_available: !!transportHealthState.dict_api_available,
    dict_ready: !!transportHealthState.dict_ready,
    last_probe_ok: !!transportHealthState.last_probe_ok,
    last_probe_at_ms: transportHealthState.last_probe_at_ms || 0,
    last_error: transportHealthState.last_error || "",
    last_probe_id: transportHealthState.last_probe_id || "",
    consecutive_failures: transportHealthState.consecutive_failures || 0,
    circuit_open_until_ms: transportHealthState.circuit_open_until_ms || 0,
    probe_interval_ms: TRANSPORT_HEALTH_PROBE_INTERVAL_MS,
    cooldown_ms: TRANSPORT_FAILURE_COOLDOWN_MS,
    failure_threshold: TRANSPORT_FAILURE_THRESHOLD,
    handoff_stats: handoff
  };
}

function _handoff_stats_mutable() {
  if (!transportHealthState.handoff_stats || typeof transportHealthState.handoff_stats !== "object") {
    transportHealthState.handoff_stats = {
      dict_attempts: 0,
      dict_successes: 0,
      dict_failures: 0,
      last_handoff_mode: ""
    };
  }
  return transportHealthState.handoff_stats;
}

function transport_handoff_snapshot() {
  const stats = _handoff_stats_mutable();
  const dictSuccesses = Math.max(0, Number(stats.dict_successes || 0));
  return {
    dict_attempts: Math.max(0, Number(stats.dict_attempts || 0)),
    dict_successes: dictSuccesses,
    dict_failures: Math.max(0, Number(stats.dict_failures || 0)),
    total_successes: dictSuccesses,
    last_handoff_mode: String(stats.last_handoff_mode || "")
  };
}

function record_handoff_stat(name) {
  const stats = _handoff_stats_mutable();
  const current = Math.max(0, Number(stats[name] || 0));
  stats[name] = current + 1;
}

function record_handoff_mode(mode) {
  const stats = _handoff_stats_mutable();
  stats.last_handoff_mode = String(mode || "");
}

function build_bridge_node_hello_payload(reason, socketId) {
  return {
    protocol_version: "2.0",
    bridge_proto: BRIDGE_PROTO,
    node_bridge_build_id: NODE_BRIDGE_BUILD_ID,
    transport_mode: TRANSPORT_DICT_REF,
    stream_kind: STREAM_KIND_HELLO,
    dict_api_available: supports_dict_api(),
    reason: String(reason || "connect"),
    socket_id: String(socketId || ""),
    timestamp_ms: now_ms(),
    transport_health: transport_health_snapshot()
  };
}

function emit_bridge_node_hello(socket, reason) {
  const payload = build_bridge_node_hello_payload(
    reason,
    socket && socket.id ? socket.id : ""
  );
  if (socket && typeof socket.emit === "function") {
    socket.emit(BRIDGE_NODE_HELLO_EVENT, payload);
  }
  try {
    io.of(NAMESPACE).emit(BRIDGE_NODE_HELLO_EVENT, payload);
  } catch (_emit_error) {
    // Best effort only.
  }
}

function mark_transport_success(probeId) {
  transportHealthState.dict_api_available = supports_dict_api();
  transportHealthState.dict_ready = true;
  transportHealthState.last_probe_ok = true;
  transportHealthState.last_probe_at_ms = now_ms();
  transportHealthState.last_error = "";
  transportHealthState.last_probe_id = String(probeId || transportHealthState.last_probe_id || "");
  transportHealthState.consecutive_failures = 0;
  transportHealthState.circuit_open_until_ms = 0;
}

function mark_transport_failure(reason, probeId) {
  const now = now_ms();
  transportHealthState.dict_api_available = supports_dict_api();
  transportHealthState.dict_ready = false;
  transportHealthState.last_probe_ok = false;
  transportHealthState.last_probe_at_ms = now;
  transportHealthState.last_error = normalize_reason(reason || "unknown transport failure");
  transportHealthState.last_probe_id = String(probeId || transportHealthState.last_probe_id || "");
  transportHealthState.consecutive_failures = (transportHealthState.consecutive_failures || 0) + 1;
  if (transportHealthState.consecutive_failures >= TRANSPORT_FAILURE_THRESHOLD) {
    transportHealthState.circuit_open_until_ms = now + TRANSPORT_FAILURE_COOLDOWN_MS;
  }
}

function transport_probe_dict_name(probeId) {
  return build_transport_dict_name("probe", "bridge", probeId || build_transport_id("bridge"));
}

async function clear_transport_dict_safe(dictName) {
  if (!dictName || !supports_dict_api()) {
    return;
  }
  try {
    await Max.setDict(String(dictName), {});
  } catch (_clear_error) {
    // Best effort only.
  }
}

function normalize_request_id(value) {
  if (value === undefined || value === null) {
    return "";
  }
  return String(value).trim();
}

function lookup_socket(socketId) {
  if (!socketId || !io) {
    return null;
  }
  const namespace = io.of(NAMESPACE);
  if (!namespace || !namespace.sockets || typeof namespace.sockets.get !== "function") {
    return null;
  }
  return namespace.sockets.get(String(socketId)) || null;
}

function cleanup_inflight_entry(entry) {
  if (!entry || typeof entry !== "object") {
    return;
  }
  if (entry.dict_name) {
    clear_transport_dict_safe(entry.dict_name);
  }
}

function emit_payload_to_origin(requestId, payload) {
  const normalizedRequestId = normalize_request_id(
    requestId || (payload && payload.request_id)
  );
  if (!normalizedRequestId) {
    return false;
  }
  const tracked = inflightRequests.get(normalizedRequestId);
  if (!tracked) {
    return false;
  }
  inflightRequests.delete(normalizedRequestId);
  cleanup_inflight_entry(tracked);
  const socket = lookup_socket(tracked.socket_id);
  if (!socket || socket.connected === false) {
    return false;
  }
  socket.emit("response", payload);
  return true;
}

function register_inflight_request(socket, outbound, meta) {
  const requestId = normalize_request_id(outbound && outbound.request_id);
  if (!requestId) {
    return {
      ok: false,
      code: "TRANSPORT_PROTOCOL_ERROR",
      message: "Bridge request is missing request_id.",
      details: {
        inflight_requests: inflightRequests.size,
        max_inflight_requests: TRANSPORT_MAX_INFLIGHT
      }
    };
  }
  if (inflightRequests.has(requestId)) {
    return {
      ok: false,
      code: "TRANSPORT_PROTOCOL_ERROR",
      message: "Duplicate in-flight request_id encountered in Node bridge.",
      details: {
        request_id: requestId,
        inflight_requests: inflightRequests.size,
        max_inflight_requests: TRANSPORT_MAX_INFLIGHT
      }
    };
  }
  if (inflightRequests.size >= TRANSPORT_MAX_INFLIGHT) {
    return {
      ok: false,
      code: ERROR_CODES.overloaded,
      message: "Node bridge in-flight request limit reached.",
      details: {
        request_id: requestId,
        inflight_requests: inflightRequests.size,
        max_inflight_requests: TRANSPORT_MAX_INFLIGHT
      }
    };
  }

  inflightRequests.set(requestId, {
    request_id: requestId,
    socket_id: socket && socket.id ? String(socket.id) : "",
    event_name: meta && meta.event_name ? String(meta.event_name) : "",
    requested_at_ms: now_ms(),
    dict_name: meta && meta.dict_name ? String(meta.dict_name) : "",
    transport_id: meta && meta.transport_id ? String(meta.transport_id) : "",
    request_chars: meta && meta.request_chars ? Number(meta.request_chars) : 0
  });
  return { ok: true, request_id: requestId };
}

function release_socket_inflight_requests(socketId) {
  if (!socketId) {
    return;
  }
  const normalizedSocketId = String(socketId);
  for (const [requestId, entry] of inflightRequests.entries()) {
    if (!entry || entry.socket_id !== normalizedSocketId) {
      continue;
    }
    inflightRequests.delete(requestId);
    cleanup_inflight_entry(entry);
  }
}

function sweep_stale_inflight_requests() {
  const now = now_ms();
  for (const [requestId, entry] of inflightRequests.entries()) {
    const requestedAt = entry && entry.requested_at_ms ? Number(entry.requested_at_ms) : 0;
    if (requestedAt <= 0 || now - requestedAt < TRANSPORT_INFLIGHT_TTL_MS) {
      continue;
    }
    inflightRequests.delete(requestId);
    cleanup_inflight_entry(entry);
    const socket = lookup_socket(entry && entry.socket_id ? entry.socket_id : "");
    if (!socket || socket.connected === false) {
      continue;
    }
    socket.emit(
      "response",
      build_failed_response(
        { request_id: requestId },
        ERROR_CODES.bridge_timeout,
        "Node bridge dropped stale in-flight request before Max response.",
        true,
        {
          request_id: requestId,
          inflight_ttl_ms: TRANSPORT_INFLIGHT_TTL_MS,
          inflight_age_ms: now - requestedAt,
          inflight_requests: inflightRequests.size,
          max_inflight_requests: TRANSPORT_MAX_INFLIGHT
        }
      )
    );
  }
}

async function probe_dict_transport(forceProbe) {
  const now = now_ms();
  const apiAvailable = supports_dict_api();
  transportHealthState.dict_api_available = apiAvailable;
  if (!apiAvailable) {
    mark_transport_failure("Node-for-Max dictionary APIs are unavailable.");
    return transport_health_snapshot();
  }

  if (!forceProbe) {
    const age = now - (transportHealthState.last_probe_at_ms || 0);
    if (age >= 0 && age < TRANSPORT_HEALTH_PROBE_INTERVAL_MS) {
      return transport_health_snapshot();
    }
    if (
      transportHealthState.circuit_open_until_ms > 0
      && now < transportHealthState.circuit_open_until_ms
    ) {
      return transport_health_snapshot();
    }
  }

  const probeId = build_transport_id("probe");
  const probeDictName = transport_probe_dict_name(probeId);
  const probePayload = {
    probe_id: probeId,
    bridge_proto: BRIDGE_PROTO,
    at_ms: now
  };
  try {
    await Max.setDict(probeDictName, probePayload);
    const loaded = await Max.getDict(probeDictName);
    const loadedProbeId = loaded && typeof loaded === "object" ? loaded.probe_id : null;
    await clear_transport_dict_safe(probeDictName);
    if (String(loadedProbeId || "") !== String(probeId)) {
      mark_transport_failure("Dictionary transport probe roundtrip mismatch.", probeId);
    } else {
      mark_transport_success(probeId);
    }
  } catch (e) {
    await clear_transport_dict_safe(probeDictName);
    mark_transport_failure(
      "Dictionary transport probe failed: " + describe_error(e),
      probeId
    );
  }
  return transport_health_snapshot();
}

function sanitize_dict_segment(value) {
  const raw = String(value || "");
  return raw.replace(/[^A-Za-z0-9_\-]/g, "_");
}

function build_transport_dict_name(kind, requestId, transportId) {
  return [
    TRANSPORT_DICT_PREFIX,
    kind || "payload",
    sanitize_dict_segment(requestId || "no_request_id"),
    sanitize_dict_segment(transportId || build_transport_id(requestId))
  ].join("_");
}

async function forward_dict_request(socket, eventName, outbound, rawLength) {
  const requestId = outbound && outbound.request_id ? String(outbound.request_id) : null;
  const transportId = build_transport_id(requestId);
  const dictName = build_transport_dict_name("req", requestId, transportId);
  const dictEnvelope = {
    ...outbound,
    bridge_proto: BRIDGE_PROTO,
    transport: TRANSPORT_DICT_REF,
    dict_ref: {
      name: dictName,
      request_id: requestId,
      kind: STREAM_KIND_REQUEST,
      expires_ms: Date.now() + TRANSPORT_DICT_TTL_MS,
      size_chars: rawLength
    }
  };

  try {
    await Max.setDict(dictName, dictEnvelope);
  } catch (e) {
    const reason = describe_error(e);
    mark_transport_failure("Request dict handoff failed: " + reason);
    Max.post(
      "[maxmcp] request_transport_v4_dict_failed request_id="
        + String(requestId)
        + " reason="
        + reason
    );
    return {
      ok: false,
      reason: reason,
      dict_name: dictName,
      request_id: requestId,
      transport_id: transportId,
      probe_id: transportHealthState.last_probe_id || ""
    };
  }

  mark_transport_success(transportHealthState.last_probe_id || build_transport_id(requestId));
  Max.post(
    "[maxmcp] request_transport_v4_dict request_id="
      + String(requestId)
      + " chars="
      + rawLength
      + " dict="
      + dictName
  );

  Max.outlet(
    "command",
    ACTION_TRANSPORT_DICT_REQUEST,
    dictName,
    requestId || "",
    transportId,
    rawLength,
    eventName || "",
    BRIDGE_PROTO
  );
  return {
    ok: true,
    reason: "",
    dict_name: dictName,
    request_id: requestId,
    transport_id: transportId,
    probe_id: transportHealthState.last_probe_id || ""
  };
}

async function forward_bridge_request(socket, eventName, data) {
  const outbound = normalize_outbound_request(data);
  const requestId = normalize_request_id(outbound && outbound.request_id);
  if (!requestId) {
    socket.emit(
      "response",
      build_failed_response(
        outbound,
        "TRANSPORT_PROTOCOL_ERROR",
        "Bridge request is missing request_id.",
        true,
        {
          event: eventName,
          inflight_requests: inflightRequests.size,
          max_inflight_requests: TRANSPORT_MAX_INFLIGHT
        }
      )
    );
    return;
  }
  let raw = "";
  try {
    raw = JSON.stringify(outbound);
  } catch (e) {
    socket.emit(
      "response",
      build_failed_response(
        outbound,
        "TRANSPORT_PROTOCOL_ERROR",
        "Unable to serialize request payload.",
        true,
        { event: eventName, reason: String(e && e.message ? e.message : e) }
      )
    );
    return;
  }

  const rawLength = raw.length;
  if (rawLength > TRANSPORT_MAX_TOTAL_CHARS) {
    socket.emit(
      "response",
      build_failed_response(
        outbound,
        "TRANSPORT_PAYLOAD_TOO_LARGE",
        "Request payload exceeds bridge transport maximum size.",
        true,
        {
          event: eventName,
          request_chars: rawLength,
          max_chars: TRANSPORT_MAX_TOTAL_CHARS,
          inflight_requests: inflightRequests.size,
          max_inflight_requests: TRANSPORT_MAX_INFLIGHT
        }
      )
    );
    return;
  }
  if (inflightRequests.has(requestId)) {
    socket.emit(
      "response",
      build_failed_response(
        outbound,
        "TRANSPORT_PROTOCOL_ERROR",
        "Duplicate in-flight request_id encountered in Node bridge.",
        true,
        {
          event: eventName,
          request_id: requestId,
          inflight_requests: inflightRequests.size,
          max_inflight_requests: TRANSPORT_MAX_INFLIGHT
        }
      )
    );
    return;
  }
  if (inflightRequests.size >= TRANSPORT_MAX_INFLIGHT) {
    socket.emit(
      "response",
      build_failed_response(
        outbound,
        ERROR_CODES.overloaded,
        "Node bridge in-flight request limit reached.",
        true,
        {
          event: eventName,
          request_id: requestId,
          inflight_requests: inflightRequests.size,
          max_inflight_requests: TRANSPORT_MAX_INFLIGHT
        }
      )
    );
    return;
  }
  const transportHealth = await probe_dict_transport(false);
  if (!transportHealth.dict_api_available) {
    socket.emit(
      "response",
      build_failed_response(
        outbound,
        "TRANSPORT_PROTOCOL_ERROR",
        "Dictionary request transport is required but unavailable in Node-for-Max runtime.",
        true,
        {
          event: eventName,
          required_transport: TRANSPORT_DICT_REF,
          request_chars: rawLength,
          dict_api_available: false,
          transport_probe_id: transportHealth.last_probe_id || "",
          dict_write_error: transportHealth.last_error || "",
          transport_health: transportHealth,
          inflight_requests: inflightRequests.size,
          max_inflight_requests: TRANSPORT_MAX_INFLIGHT
        }
      )
    );
    return;
  }

  let sent = null;
  const retryAttempts = Math.max(1, TRANSPORT_REQUEST_RETRY_ATTEMPTS);
  for (let attempt = 1; attempt <= retryAttempts; attempt++) {
    record_handoff_stat("dict_attempts");
    sent = await forward_dict_request(socket, eventName, outbound, rawLength);
    if (sent && sent.ok) {
      record_handoff_stat("dict_successes");
      record_handoff_mode(TRANSPORT_DICT_REF);
      break;
    }
    record_handoff_stat("dict_failures");
    if (attempt >= retryAttempts) {
      break;
    }
    await probe_dict_transport(true);
    if (TRANSPORT_REQUEST_RETRY_DELAY_MS > 0) {
      await sleep_ms(TRANSPORT_REQUEST_RETRY_DELAY_MS);
    }
  }
  if (!sent || !sent.ok) {
    socket.emit(
      "response",
      build_failed_response(
        outbound,
        "TRANSPORT_PROTOCOL_ERROR",
        "Failed to hand off request through dictionary transport.",
        true,
        {
          event: eventName,
          request_chars: rawLength,
          required_transport: TRANSPORT_DICT_REF,
          dict_name: sent && sent.dict_name ? sent.dict_name : "",
          dict_api_available: supports_dict_api(),
          dict_write_error: sent && sent.reason ? sent.reason : "",
          handoff_attempts: retryAttempts,
          retry_delay_ms: TRANSPORT_REQUEST_RETRY_DELAY_MS,
          transport_probe_id: sent && sent.probe_id ? sent.probe_id : "",
          transport_health: transport_health_snapshot(),
          inflight_requests: inflightRequests.size,
          max_inflight_requests: TRANSPORT_MAX_INFLIGHT
        }
      )
    );
    return;
  }

  const registered = register_inflight_request(socket, outbound, {
    event_name: eventName,
    dict_name: sent.dict_name || "",
    transport_id: sent.transport_id || "",
    request_chars: rawLength
  });
  if (!registered.ok) {
    cleanup_inflight_entry(sent);
    socket.emit(
      "response",
      build_failed_response(
        outbound,
        registered.code || "TRANSPORT_PROTOCOL_ERROR",
        registered.message || "Node bridge could not register in-flight request.",
        true,
        {
          event: eventName,
          request_chars: rawLength,
          dict_name: sent.dict_name || "",
          handoff_transport: TRANSPORT_DICT_REF,
          inflight_requests: inflightRequests.size,
          max_inflight_requests: TRANSPORT_MAX_INFLIGHT,
          ...(registered.details || {})
        }
      )
    );
    return;
  }
}

// Create Socket.IO server
var io = new Server(PORT, {
  ...build_server_options()
});

Max.outlet("port", `Server listening on port ${PORT}`);
const inflightSweepHandle = setInterval(() => {
  try {
    sweep_stale_inflight_requests();
  } catch (e) {
    Max.post(
      "[maxmcp] inflight_sweep_error " + String(e && e.message ? e.message : e)
    );
  }
}, TRANSPORT_INFLIGHT_SWEEP_MS);
if (inflightSweepHandle && typeof inflightSweepHandle.unref === "function") {
  inflightSweepHandle.unref();
}

function safe_parse_json(str) {
    try {
        return JSON.parse(str);
    } catch (e) {
        Max.post("error, Invalid JSON: " + e.message);
        return null;
    }
}

function normalize_response_payload(msg) {
  if (!Array.isArray(msg) || msg.length === 0) {
    return "";
  }
  if (msg.length === 1 && Array.isArray(msg[0])) {
    return msg[0].join("");
  }
  if (msg.length === 1 && typeof msg[0] === "string") {
    return msg[0];
  }
  let out = "";
  for (let i = 0; i < msg.length; i++) {
    const part = msg[i];
    if (Array.isArray(part)) {
      out += part.join("");
    } else if (typeof part === "string") {
      out += part;
    } else if (part !== undefined && part !== null) {
      out += String(part);
    }
  }
  return out;
}

function emit_transport_failure_to_clients(data, code, message, details) {
  const failure = build_failed_response(
    data && typeof data === "object" ? data : {},
    code,
    message,
    true,
    details || {}
  );
  const delivered = emit_payload_to_origin(
    normalize_request_id(failure.request_id),
    failure
  );
  if (!delivered) {
    io.of(NAMESPACE).emit("response", failure);
  }
}

async function clear_transport_dict(dictName) {
  if (!dictName || !supports_dict_api()) {
    return;
  }
  try {
    await Max.setDict(String(dictName), {});
  } catch (_clear_error) {
    // Best effort only.
  }
}

async function handle_dict_response_reference(
  dictName,
  requestId,
  transportId,
  totalChars,
  bridgeProto
) {
  const normalizedRequestId = requestId ? String(requestId) : null;
  const normalizedDictName = dictName ? String(dictName) : "";
  const normalizedTransportId = transportId ? String(transportId) : "";
  const expectedChars = parseInt(totalChars, 10);
  const proto = bridgeProto ? String(bridgeProto) : BRIDGE_PROTO;

  if (proto !== BRIDGE_PROTO) {
    emit_transport_failure_to_clients(
      { request_id: normalizedRequestId },
      "BRIDGE_PROTOCOL_MISMATCH",
      "Bridge protocol mismatch in dict response transport.",
      {
        expected: BRIDGE_PROTO,
        received: proto,
        dict_name: normalizedDictName,
        transport_id: normalizedTransportId
      }
    );
    return;
  }
  if (!normalizedDictName) {
    emit_transport_failure_to_clients(
      { request_id: normalizedRequestId },
      "TRANSPORT_PROTOCOL_ERROR",
      "Missing dict transport reference in response.",
      {
        dict_name: normalizedDictName,
        transport_id: normalizedTransportId
      }
    );
    return;
  }
  if (!supports_dict_api()) {
    emit_transport_failure_to_clients(
      { request_id: normalizedRequestId },
      "TRANSPORT_PROTOCOL_ERROR",
      "Node-for-Max dictionary APIs are unavailable.",
      {
        dict_name: normalizedDictName,
        transport_id: normalizedTransportId
      }
    );
    return;
  }

  let payload = null;
  try {
    payload = await Max.getDict(normalizedDictName);
  } catch (e) {
    mark_transport_failure(
      "Unable to resolve dict response payload: "
        + String(e && e.message ? e.message : e)
    );
    emit_transport_failure_to_clients(
      { request_id: normalizedRequestId },
      "TRANSPORT_PROTOCOL_ERROR",
      "Unable to resolve dict response payload.",
      {
        dict_name: normalizedDictName,
        transport_id: normalizedTransportId,
        reason: String(e && e.message ? e.message : e)
      }
    );
    return;
  } finally {
    await clear_transport_dict(normalizedDictName);
  }

  if (!payload || typeof payload !== "object") {
    mark_transport_failure("Resolved dict response payload is not an object.");
    emit_transport_failure_to_clients(
      { request_id: normalizedRequestId },
      "TRANSPORT_CORRUPT_PAYLOAD",
      "Resolved dict response payload is not an object.",
      {
        dict_name: normalizedDictName,
        transport_id: normalizedTransportId,
        payload_type: String(typeof payload)
      }
    );
    return;
  }

  mark_transport_success(transportHealthState.last_probe_id || build_transport_id(normalizedRequestId));
  if (payload.bridge_proto && payload.bridge_proto !== BRIDGE_PROTO) {
    emit_transport_failure_to_clients(
      { request_id: payload.request_id || normalizedRequestId },
      "BRIDGE_PROTOCOL_MISMATCH",
      "Bridge protocol mismatch in dict response payload.",
      {
        expected: BRIDGE_PROTO,
        received: payload.bridge_proto,
        dict_name: normalizedDictName,
        transport_id: normalizedTransportId
      }
    );
    return;
  }
  if (
    normalizedRequestId
    && payload.request_id
    && String(payload.request_id) !== normalizedRequestId
  ) {
    emit_transport_failure_to_clients(
      { request_id: normalizedRequestId },
      "TRANSPORT_PROTOCOL_ERROR",
      "Dict response request_id mismatch.",
      {
        expected_request_id: normalizedRequestId,
        received_request_id: String(payload.request_id),
        dict_name: normalizedDictName,
        transport_id: normalizedTransportId
      }
    );
    return;
  }

  if (isFinite(expectedChars) && expectedChars >= 0) {
    try {
      const serialized = JSON.stringify(payload);
      if (serialized.length > expectedChars + 1024) {
        emit_transport_failure_to_clients(
          { request_id: payload.request_id || normalizedRequestId },
          "TRANSPORT_PROTOCOL_ERROR",
          "Dict response payload length exceeded expected size.",
          {
            dict_name: normalizedDictName,
            transport_id: normalizedTransportId,
            expected_chars: expectedChars,
            received_chars: serialized.length
          }
        );
        return;
      }
    } catch (_stringify_error) {
      // No-op, response object itself is still usable.
    }
  }

  const delivered = emit_payload_to_origin(
    normalize_request_id(payload.request_id || normalizedRequestId),
    payload
  );
  if (!delivered) {
    io.of(NAMESPACE).emit("response", payload);
  }
}

function normalize_atom_args(msg) {
  if (!Array.isArray(msg) || msg.length === 0) {
    return [];
  }
  if (msg.length === 1 && Array.isArray(msg[0])) {
    return msg[0];
  }
  return msg;
}

Max.addHandler(ACTION_TRANSPORT_DICT_RESPONSE, async (...msg) => {
  const args = normalize_atom_args(msg);
  if (!Array.isArray(args) || args.length === 0) {
    emit_transport_failure_to_clients(
      { request_id: null },
      "TRANSPORT_PROTOCOL_ERROR",
      "Invalid dict response transport control message.",
      { args_count: Array.isArray(args) ? args.length : 0 }
    );
    return;
  }
  const dictName = args[0] !== undefined && args[0] !== null ? String(args[0]) : "";
  const requestId = args.length > 1 ? String(args[1] || "") : "";
  const transportId = args.length > 2 ? String(args[2] || "") : "";
  const totalChars = args.length > 3 ? parseInt(args[3], 10) : -1;
  const bridgeProto = args.length > 4 ? String(args[4] || "") : BRIDGE_PROTO;
  await handle_dict_response_reference(
    dictName,
    requestId,
    transportId,
    totalChars,
    bridgeProto
  );
});

Max.addHandler("response", async (...msg) => {
	var str = normalize_response_payload(msg);
	var data = safe_parse_json(str);
    if (!data || typeof data !== "object") {
      return;
    }
    if (data.bridge_proto && data.bridge_proto !== BRIDGE_PROTO) {
      emit_transport_failure_to_clients(
        { request_id: data.request_id || null },
        "BRIDGE_PROTOCOL_MISMATCH",
        "Bridge protocol mismatch in envelope response.",
        {
          expected: BRIDGE_PROTO,
          received: data.bridge_proto
        }
      );
      return;
    }
  const delivered = emit_payload_to_origin(
    normalize_request_id(data.request_id),
    data
  );
  if (!delivered) {
	  await io.of(NAMESPACE).emit("response", data);
  }
});

Max.addHandler("port", async (msg) => {
  Max.post(`msg ${msg}`);
  if (msg > 0 && msg < 65536) {
    PORT = msg;
  }
  await io.close();
  io = new Server(PORT, {
    ...build_server_options()
  });
  // await Max.post(`Socket.IO MCP server listening on port ${PORT}`);
  await Max.outlet("port", `Server listening on port ${PORT}`);
});

io.of(NAMESPACE).on("connection", (socket) => {
  const remoteAddress = String(socket.handshake && socket.handshake.address ? socket.handshake.address : "");
  if (!MAXMCP_ALLOW_REMOTE && !is_local_address(remoteAddress)) {
    Max.post(`Rejected non-local MCP client: ${socket.id} (${remoteAddress})`);
    socket.emit(
      "response",
      build_unauthorized_response(
        { request_id: null },
        "Remote MCP clients are disabled. Set MAXMCP_ALLOW_REMOTE=1 to allow remote access."
      )
    );
    socket.disconnect(true);
    return;
  }

  const handshakeToken = extract_handshake_token(socket);
  const handshakeAuthorized = is_authorized_token(handshakeToken);
  socket.data = socket.data || {};
  socket.data.handshakeAuthorized = handshakeAuthorized;
  if (MAXMCP_AUTH_TOKEN && MAXMCP_REQUIRE_HANDSHAKE_AUTH && !handshakeAuthorized) {
    Max.post(`Rejected unauthenticated MCP client: ${socket.id}`);
    socket.emit(
      "response",
      build_unauthorized_response(
        { request_id: null },
        "Handshake authentication required. Provide token in Socket.IO auth.token or x-maxmcp-token header."
      )
    );
    socket.disconnect(true);
    return;
  }

  Max.post(`Socket.IO client connected: ${socket.id}`);
  emit_bridge_node_hello(socket, "socket_connected");
  probe_dict_transport(true).catch(() => {});

  socket.on("command", async (data) => {
    if (reject_if_unauthorized(socket, data)) {
      return;
    }
    try {
      await forward_bridge_request(socket, "command", data);
    } catch (e) {
      socket.emit(
        "response",
        build_failed_response(
          data && typeof data === "object" ? data : {},
          "TRANSPORT_PROTOCOL_ERROR",
          "Unhandled bridge forwarding error.",
          true,
          { event: "command", reason: String(e && e.message ? e.message : e) }
        )
      );
    }
  });

  socket.on("request", async (data) => {
    if (reject_if_unauthorized(socket, data)) {
      return;
    }
    // Route requests through same outlet as commands - js handles both.
    try {
      await forward_bridge_request(socket, "request", data);
    } catch (e) {
      socket.emit(
        "response",
        build_failed_response(
          data && typeof data === "object" ? data : {},
          "TRANSPORT_PROTOCOL_ERROR",
          "Unhandled bridge forwarding error.",
          true,
          { event: "request", reason: String(e && e.message ? e.message : e) }
        )
      );
    }
  });

  socket.on("port", async (data) => {
    Max.post(`msg ${data}`);
    if (data > 0 && data < 65536) {
      PORT = data;
    }
    await io.close();
    io = new Server(PORT, {
      ...build_server_options()
    });
    // await Max.post(`Socket.IO MCP server listening on port ${PORT}`);
    await Max.outlet("port", `Server listening on port ${PORT}`);
  });
  

  socket.on("disconnect", () => {
    release_socket_inflight_requests(socket.id);
    Max.post(`Socket.IO client disconnected: ${socket.id}`);
  });
});
