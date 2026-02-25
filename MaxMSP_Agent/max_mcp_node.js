autowatch = 1;

const Max = require("max-api");
const { Server } = require("socket.io");
const crypto = require("crypto");
const fs = require("fs");

// Configuration
var PORT = 5002;
const NAMESPACE = "/mcp";
const MAXMCP_ALLOW_REMOTE =
  ["1", "true", "yes", "on"].indexOf(
    String(process.env.MAXMCP_ALLOW_REMOTE || "").toLowerCase()
  ) !== -1;
const MAXMCP_AUTH_TOKEN_FILE = String(process.env.MAXMCP_AUTH_TOKEN_FILE || "").trim();
const MAXMCP_REQUIRE_HANDSHAKE_AUTH =
  ["1", "true", "yes", "on"].indexOf(
    String(process.env.MAXMCP_REQUIRE_HANDSHAKE_AUTH || "true").toLowerCase()
  ) !== -1;

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
    request_id: reqId,
    state: "failed",
    timestamp_ms: Date.now(),
    error: {
      code: "UNAUTHORIZED",
      message: message || "Authentication token missing or invalid.",
      recoverable: true,
      details: {}
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

// Create Socket.IO server
var io = new Server(PORT, {
  ...build_server_options()
});

Max.outlet("port", `Server listening on port ${PORT}`);

function safe_parse_json(str) {
    try {
        return JSON.parse(str);
    } catch (e) {
        Max.post("error, Invalid JSON: " + e.message);
        Max.post("This is likely because the patcher has too much objects, select some of them and try again");
        return null;
    }
}

Max.addHandler("response", async (...msg) => {
	var str = msg.join("")
	var data = safe_parse_json(str);
    if (!data) {
      return;
    }
	await io.of(NAMESPACE).emit("response", data);
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

  socket.on("command", async (data) => {
    if (reject_if_unauthorized(socket, data)) {
      return;
    }
	  Max.outlet("command", JSON.stringify(data));
  });

  socket.on("request", async (data) => {
    if (reject_if_unauthorized(socket, data)) {
      return;
    }
    // Route requests through same outlet as commands - js handles both
	  Max.outlet("command", JSON.stringify(data));
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
    Max.post(`Socket.IO client disconnected: ${socket.id}`);
  });
});
