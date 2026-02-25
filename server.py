# server.py
from mcp.server.fastmcp import FastMCP, Context
from contextlib import asynccontextmanager
from collections import OrderedDict, deque, defaultdict
from pathlib import Path
from difflib import get_close_matches
import asyncio
import subprocess
import socketio
import signal
import shutil
try:
    import aiohttp
except Exception:  # pragma: no cover - optional dependency guard
    aiohttp = None

from typing import Any
import logging
import time
import uuid
import os
import json
import hashlib

SOCKETIO_SERVER_URL = os.environ.get("SOCKETIO_SERVER_URL", "http://127.0.0.1")
SOCKETIO_SERVER_PORT = os.environ.get("SOCKETIO_SERVER_PORT", "5002")
NAMESPACE = os.environ.get("NAMESPACE", "/mcp")

current_dir = os.path.dirname(os.path.abspath(__file__))
MAX_APP_PATH = Path(os.environ.get("MAXMCP_MAX_APP", "/Applications/Max.app"))
HOST_PATCH_PATH = Path(
    os.environ.get(
        "MAXMCP_HOST_PATCH", os.path.join(current_dir, "MaxMSP_Agent", "mcp_host.maxpat")
    )
)
FALLBACK_PATCH_PATH = Path(os.path.join(current_dir, "MaxMSP_Agent", "demo.maxpat"))
MAXMCP_STATE_DIR = Path(
    os.path.expanduser(os.environ.get("MAXMCP_STATE_DIR", "~/.maxmsp-mcp"))
)
MAXMCP_STATE_FILE = MAXMCP_STATE_DIR / "state.json"
MAXMCP_NPM_PROJECT_DIR = Path(os.path.join(current_dir, "MaxMSP_Agent"))
MAXMCP_NPM_SENTINEL = MAXMCP_NPM_PROJECT_DIR / "node_modules" / "socket.io"
PROTECTED_VARNAME_PREFIX = "__maxmcp_bridge_"
PROTOCOL_VERSION = "2.0"
MAXMCP_HEARTBEAT_INTERVAL_SECONDS = float(
    os.environ.get("MAXMCP_HEARTBEAT_INTERVAL_SECONDS", "10")
)
MAXMCP_STALE_THRESHOLD_SECONDS = float(
    os.environ.get("MAXMCP_STALE_THRESHOLD_SECONDS", "30")
)
MAXMCP_IDEMPOTENCY_CACHE_SIZE = int(os.environ.get("MAXMCP_IDEMPOTENCY_CACHE_SIZE", "512"))
MAXMCP_SESSIONS_ROOT = Path(
    os.environ.get(
        "MAXMCP_SESSIONS_ROOT",
        os.path.join(current_dir, "target", "maxmcp", "sessions"),
    )
)
MAXMCP_SESSION_ID = os.environ.get("MAXMCP_SESSION_ID", uuid.uuid4().hex[:12])
MAXPYLANG_ROOT = Path(
    os.environ.get(
        "MAXMCP_MAXPY_ROOT",
        os.path.join(current_dir, "refs", "MaxPyLang-main", "maxpylang"),
    )
)
MAXPYLANG_TEMPLATE_PATH = MAXPYLANG_ROOT / "data" / "PATCH_TEMPLATES" / "empty_template.json"
MAXMCP_CHECKPOINT_MAX = int(os.environ.get("MAXMCP_CHECKPOINT_MAX", "20"))

ERROR_BRIDGE_UNAVAILABLE = "BRIDGE_UNAVAILABLE"
ERROR_BRIDGE_TIMEOUT = "BRIDGE_TIMEOUT"
ERROR_VALIDATION = "VALIDATION_ERROR"
ERROR_INTERNAL = "INTERNAL_ERROR"
ERROR_OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"
ERROR_PROTECTED_OBJECT = "PROTECTED_OBJECT"
ERROR_UNKNOWN_ACTION = "UNKNOWN_ACTION"
ERROR_PRECONDITION = "PRECONDITION_FAILED"
ERROR_OVERLOADED = "OVERLOADED"
ERROR_UNAUTHORIZED = "UNAUTHORIZED"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


MAXMCP_MANAGED_MODE = _env_bool("MAXMCP_MANAGED_MODE", True)
MAXMCP_NPM_AUTO_INSTALL = _env_bool("MAXMCP_NPM_AUTO_INSTALL", True)
MAXMCP_TWIN_AUTO_SYNC = _env_bool("MAXMCP_TWIN_AUTO_SYNC", True)
MAXMCP_STRICT_V2_ENFORCEMENT = _env_bool("MAXMCP_STRICT_V2_ENFORCEMENT", True)
MAXMCP_STRICT_CAPABILITY_GATING = _env_bool("MAXMCP_STRICT_CAPABILITY_GATING", True)
MAXMCP_REQUIRE_HANDSHAKE_AUTH = _env_bool("MAXMCP_REQUIRE_HANDSHAKE_AUTH", True)
MAXMCP_AUTH_TOKEN_FILE = Path(
    os.path.expanduser(
        os.environ.get("MAXMCP_AUTH_TOKEN_FILE", "~/.maxmsp-mcp/auth_token")
    )
)
MAXMCP_MUTATION_MAX_INFLIGHT = int(os.environ.get("MAXMCP_MUTATION_MAX_INFLIGHT", "4"))
MAXMCP_MUTATION_MAX_QUEUE = int(os.environ.get("MAXMCP_MUTATION_MAX_QUEUE", "64"))
MAXMCP_MUTATION_QUEUE_WAIT_TIMEOUT_SECONDS = float(
    os.environ.get("MAXMCP_MUTATION_QUEUE_WAIT_TIMEOUT_SECONDS", "15")
)
MAXMCP_METRICS_SAMPLE_SIZE = int(os.environ.get("MAXMCP_METRICS_SAMPLE_SIZE", "512"))
MAXMCP_EVENT_LOG_SIZE = int(os.environ.get("MAXMCP_EVENT_LOG_SIZE", "256"))
MAXMCP_METRICS_LOG_INTERVAL_SECONDS = float(
    os.environ.get("MAXMCP_METRICS_LOG_INTERVAL_SECONDS", "30")
)
MAXMCP_ALERT_FAILURE_RATE = float(os.environ.get("MAXMCP_ALERT_FAILURE_RATE", "0.10"))
MAXMCP_ALERT_P95_MS = float(os.environ.get("MAXMCP_ALERT_P95_MS", "1500"))
MAXMCP_ALERT_QUEUE_DEPTH = float(os.environ.get("MAXMCP_ALERT_QUEUE_DEPTH", "0.80"))
MAXMCP_ALERT_WINDOW_SECONDS = float(os.environ.get("MAXMCP_ALERT_WINDOW_SECONDS", "300"))
MAXMCP_ENFORCE_PATCH_ROOTS = _env_bool("MAXMCP_ENFORCE_PATCH_ROOTS", False)
MAXMCP_ALLOWED_PATCH_ROOTS_RAW = os.environ.get("MAXMCP_ALLOWED_PATCH_ROOTS", "").strip()
MAXMCP_HYGIENE_AUTO_CLEANUP = _env_bool("MAXMCP_HYGIENE_AUTO_CLEANUP", True)
MAXMCP_HYGIENE_SCOPE = os.environ.get("MAXMCP_HYGIENE_SCOPE", "all_max_instances").strip()
MAXMCP_HYGIENE_MODE = os.environ.get("MAXMCP_HYGIENE_MODE", "aggressive").strip()
MAXMCP_HYGIENE_STALE_SECONDS = int(os.environ.get("MAXMCP_HYGIENE_STALE_SECONDS", "1800"))
MAXMCP_HYGIENE_STARTUP_SWEEP = _env_bool("MAXMCP_HYGIENE_STARTUP_SWEEP", True)
MAXMCP_HYGIENE_REPORT_MAX = int(os.environ.get("MAXMCP_HYGIENE_REPORT_MAX", "500"))
MAXMCP_HYGIENE_MAX_KILLS_PER_SWEEP = int(
    os.environ.get("MAXMCP_HYGIENE_MAX_KILLS_PER_SWEEP", "50")
)
MAXMCP_HYGIENE_ENABLE_WINDOW_SCAN = _env_bool("MAXMCP_HYGIENE_ENABLE_WINDOW_SCAN", True)
MAXMCP_HYGIENE_LOOP_INTERVAL_SECONDS = float(
    os.environ.get("MAXMCP_HYGIENE_LOOP_INTERVAL_SECONDS", "60")
)
MAXMCP_HYGIENE_KEEP_RECENT_SESSIONS = int(
    os.environ.get("MAXMCP_HYGIENE_KEEP_RECENT_SESSIONS", "2")
)


def _resolve_auth_token_from_sources(
    env_token: str | None,
    token_file: Path,
) -> tuple[str, str]:
    token = (env_token or "").strip()
    if token:
        return token, "env"

    try:
        if token_file.exists():
            from_file = token_file.read_text(encoding="utf-8").strip()
            if from_file:
                return from_file, "file"
    except Exception:
        pass
    return "", "none"


MAXMCP_AUTH_TOKEN, MAXMCP_AUTH_TOKEN_SOURCE = _resolve_auth_token_from_sources(
    os.environ.get("MAXMCP_AUTH_TOKEN", ""),
    MAXMCP_AUTH_TOKEN_FILE,
)


def _parse_path_roots(raw: str) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for piece in raw.split(os.pathsep):
        candidate = piece.strip()
        if not candidate:
            continue
        resolved = Path(candidate).expanduser()
        if not resolved.is_absolute():
            resolved = (Path(current_dir) / resolved).resolve()
        else:
            resolved = resolved.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


MAXMCP_ALLOWED_PATCH_ROOTS = _parse_path_roots(MAXMCP_ALLOWED_PATCH_ROOTS_RAW)

docs_path = os.path.join(current_dir, "docs.json")
with open(docs_path, "r") as f:
    docs = json.load(f)
flattened_docs = {}
for obj_list in docs.values():
    for obj in obj_list:
        flattened_docs[obj["name"]] = obj


class MaxPyCatalog:
    """Read-only index over vendored MaxPyLang object metadata."""

    def __init__(self, root: Path):
        self.root = root
        self.obj_info_dir = self.root / "data" / "OBJ_INFO"
        self.aliases_file = self.obj_info_dir / "obj_aliases.json"
        self._index: dict[str, dict] = {}
        self._aliases: dict[str, str] = {}
        self._reverse_aliases: dict[str, list[str]] = {}
        self._schema_cache: dict[str, dict] = {}
        self.available = False
        self.schema_hash = ""
        self._init()

    def _init(self) -> None:
        if not self.obj_info_dir.exists():
            return

        digest = hashlib.sha256()
        files = sorted(self.obj_info_dir.glob("*/*.json"))
        for file_path in files:
            name = file_path.stem
            package = file_path.parent.name
            self._index[name] = {
                "name": name,
                "package": package,
                "path": file_path,
            }
            digest.update(file_path.relative_to(self.obj_info_dir).as_posix().encode("utf-8"))
            st = file_path.stat()
            digest.update(f":{st.st_size}:{int(st.st_mtime)}".encode("utf-8"))

        if self.aliases_file.exists():
            try:
                alias_payload = json.loads(self.aliases_file.read_text())
                if isinstance(alias_payload, dict):
                    self._aliases = {str(k): str(v) for k, v in alias_payload.items()}
                digest.update(self.aliases_file.read_bytes())
            except Exception:
                self._aliases = {}

        for alias, canonical in self._aliases.items():
            self._reverse_aliases.setdefault(canonical, []).append(alias)

        self.schema_hash = digest.hexdigest()[:16]
        self.available = len(self._index) > 0

    @property
    def count(self) -> int:
        return len(self._index)

    @property
    def packages(self) -> list[str]:
        return sorted({entry["package"] for entry in self._index.values()})

    def resolve_name(self, object_name: str) -> tuple[str, bool]:
        canonical = self._aliases.get(object_name, object_name)
        return canonical, canonical != object_name

    def aliases_for(self, object_name: str) -> list[str]:
        return sorted(self._reverse_aliases.get(object_name, []))

    def _load_schema(self, object_name: str) -> dict | None:
        if object_name in self._schema_cache:
            return self._schema_cache[object_name]

        entry = self._index.get(object_name)
        if not entry:
            return None
        try:
            schema = json.loads(entry["path"].read_text())
            if isinstance(schema, dict):
                self._schema_cache[object_name] = schema
                return schema
        except Exception:
            return None
        return None

    def get_schema(self, object_name: str) -> dict | None:
        canonical, via_alias = self.resolve_name(object_name)
        entry = self._index.get(canonical)
        if not entry:
            return None
        schema = self._load_schema(canonical)
        if not isinstance(schema, dict):
            return None
        default_box = schema.get("default", {}).get("box", {})
        return {
            "query": object_name,
            "canonical_name": canonical,
            "resolved_via_alias": via_alias,
            "package": entry["package"],
            "path": str(entry["path"]),
            "aliases": self.aliases_for(canonical),
            "summary": {
                "maxclass": default_box.get("maxclass"),
                "numinlets": default_box.get("numinlets"),
                "numoutlets": default_box.get("numoutlets"),
                "default_text": default_box.get("text"),
            },
            "schema": schema,
        }

    def suggest(self, object_name: str, limit: int = 5) -> list[str]:
        universe = sorted(set(self._index.keys()) | set(self._aliases.keys()))
        return get_close_matches(object_name, universe, n=limit, cutoff=0.6)

    def search(
        self,
        query: str,
        *,
        package: str | None = None,
        limit: int = 20,
        include_aliases: bool = True,
    ) -> list[dict]:
        query_lc = query.lower().strip()
        if not query_lc:
            return []

        rows: list[tuple[int, str, dict]] = []
        for name, entry in self._index.items():
            if package and entry["package"] != package:
                continue
            score = None
            name_lc = name.lower()
            if name_lc == query_lc:
                score = 0
            elif name_lc.startswith(query_lc):
                score = 1
            elif query_lc in name_lc:
                score = 2

            alias_matches: list[str] = []
            if include_aliases:
                for alias in self._reverse_aliases.get(name, []):
                    alias_lc = alias.lower()
                    if alias_lc == query_lc:
                        alias_matches.append(alias)
                        score = min(score if score is not None else 3, 0)
                    elif alias_lc.startswith(query_lc):
                        alias_matches.append(alias)
                        score = min(score if score is not None else 3, 1)
                    elif query_lc in alias_lc:
                        alias_matches.append(alias)
                        score = min(score if score is not None else 3, 2)

            if score is None:
                continue

            rows.append(
                (
                    score,
                    name,
                    {
                        "name": name,
                        "package": entry["package"],
                        "aliases": sorted(alias_matches),
                    },
                )
            )

        rows.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in rows[: max(1, min(limit, 100))]]

    def io_counts(self, object_name: str) -> tuple[int | None, int | None]:
        schema_info = self.get_schema(object_name)
        if not schema_info:
            return None, None
        summary = schema_info.get("summary", {})
        numinlets = summary.get("numinlets")
        numoutlets = summary.get("numoutlets")
        if not isinstance(numinlets, int):
            numinlets = None
        if not isinstance(numoutlets, int):
            numoutlets = None
        return numinlets, numoutlets


maxpy_catalog = MaxPyCatalog(MAXPYLANG_ROOT)


class MaxMCPError(RuntimeError):
    """Canonical bridge error with structured metadata."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        recoverable: bool = True,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.recoverable = recoverable
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
            "details": self.details,
        }
        if self.hint:
            payload["hint"] = self.hint
        return payload


def _error_result(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    recoverable: bool = True,
    details: dict | None = None,
) -> dict:
    error = {
        "code": code,
        "message": message,
        "recoverable": recoverable,
        "details": details or {},
    }
    if hint:
        error["hint"] = hint
    return {"success": False, "error": error}


def _normalize_legacy_error(message: str, hint: str | None = None) -> dict:
    return _error_result(ERROR_VALIDATION, message, hint=hint, recoverable=True)


UNGATED_ACTIONS = {"capabilities", "health_ping", "bridge_ping"}
MUTATING_BRIDGE_ACTIONS = {
    "add_object",
    "remove_object",
    "connect_objects",
    "disconnect_objects",
    "set_object_attribute",
    "set_message_text",
    "send_message_to_object",
    "send_bang_to_object",
    "set_number",
    "create_subpatcher",
    "enter_subpatcher",
    "exit_subpatcher",
    "add_subpatcher_io",
    "recreate_with_args",
    "move_object",
    "autofit_existing",
    "encapsulate",
    "set_workspace_target",
    "apply_topology_snapshot",
}
BULK_BRIDGE_ACTIONS = {
    "get_objects_in_patch",
    "get_objects_in_selected",
    "apply_topology_snapshot",
}


class MaxMSPConnection:
    def __init__(self, server_url: str, server_port: str, namespace: str = NAMESPACE):

        self.server_url = server_url
        self.server_port = server_port
        self.namespace = namespace
        self.endpoint = f"{self.server_url}:{self.server_port}{self.namespace}"
        self.last_connect_error: str | None = None
        self.runtime_manager = None

        sio_options: dict[str, Any] = {}
        if aiohttp is not None and hasattr(aiohttp, "ClientWSTimeout"):
            sio_options["websocket_extra_options"] = {
                # aiohttp >= 3.11 deprecates float timeout; pass ClientWSTimeout.
                "timeout": aiohttp.ClientWSTimeout(ws_close=10.0),
            }
        self.sio = socketio.AsyncClient(**sio_options)
        self._pending: dict[str, asyncio.Future] = {}
        self._idempotent_results: OrderedDict[str, Any] = OrderedDict()
        self.max_idempotency_cache_size = MAXMCP_IDEMPOTENCY_CACHE_SIZE
        self.protocol_version = PROTOCOL_VERSION
        self.capabilities: dict = {}

        self.connected_at: float | None = None
        self.last_request_at: float | None = None
        self.last_response_at: float | None = None
        self.last_successful_request_at: float | None = None
        self.last_failed_request_at: float | None = None
        self.last_timeout_at: float | None = None
        self.last_heartbeat_at: float | None = None
        self.last_heartbeat_error: str | None = None
        self.total_requests = 0
        self.total_successes = 0
        self.total_failures = 0
        self.total_timeouts = 0
        self.consecutive_failures = 0
        self.action_request_counts: dict[str, int] = defaultdict(int)
        self.action_failure_counts: dict[str, int] = defaultdict(int)
        self.action_timeout_counts: dict[str, int] = defaultdict(int)
        self.heartbeat_interval_seconds = MAXMCP_HEARTBEAT_INTERVAL_SECONDS
        self.stale_threshold_seconds = MAXMCP_STALE_THRESHOLD_SECONDS
        self.strict_v2_enforcement = MAXMCP_STRICT_V2_ENFORCEMENT
        self.strict_capability_gating = MAXMCP_STRICT_CAPABILITY_GATING
        self.auth_token = MAXMCP_AUTH_TOKEN
        self.auth_token_source = MAXMCP_AUTH_TOKEN_SOURCE
        self.auth_token_file = MAXMCP_AUTH_TOKEN_FILE
        self.require_handshake_auth = MAXMCP_REQUIRE_HANDSHAKE_AUTH
        self.metrics_log_interval_seconds = max(1.0, MAXMCP_METRICS_LOG_INTERVAL_SECONDS)
        self.alert_failure_rate = max(0.0, MAXMCP_ALERT_FAILURE_RATE)
        self.alert_p95_ms = max(0.0, MAXMCP_ALERT_P95_MS)
        self.alert_queue_depth = min(1.0, max(0.0, MAXMCP_ALERT_QUEUE_DEPTH))
        self.alert_window_seconds = max(30.0, MAXMCP_ALERT_WINDOW_SECONDS)
        self.last_metrics_log_emit_at: float | None = None

        self.mutation_max_inflight = max(1, MAXMCP_MUTATION_MAX_INFLIGHT)
        self.mutation_max_queue = max(1, MAXMCP_MUTATION_MAX_QUEUE)
        self.mutation_queue_wait_timeout_seconds = max(
            0.1,
            MAXMCP_MUTATION_QUEUE_WAIT_TIMEOUT_SECONDS,
        )
        self._mutation_condition = asyncio.Condition()
        self._mutation_waiters: deque[str] = deque()
        self._queued_mutation_requests = 0
        self._inflight_mutation_requests = 0
        self.max_queue_depth_seen = 0
        self.mutation_queue_rejections = 0
        self.mutation_queue_timeouts = 0
        self.total_mutation_queue_wait_seconds = 0.0

        self._latency_samples = deque(maxlen=max(8, MAXMCP_METRICS_SAMPLE_SIZE))
        self._event_log = deque(maxlen=max(8, MAXMCP_EVENT_LOG_SIZE))

        @self.sio.on("response", namespace=self.namespace)
        async def _on_response(data):
            envelope = self._normalize_response(data)
            req_id = envelope.get("request_id")
            fut = self._pending.get(req_id)
            if fut and not fut.done():
                fut.set_result(envelope)

    def _normalize_response(self, payload: Any) -> dict:
        if not isinstance(payload, dict):
            return {
                "protocol_version": self.protocol_version,
                "request_id": None,
                "state": "failed",
                "error": {
                    "code": ERROR_INTERNAL,
                    "message": "Bridge returned non-dict response payload.",
                    "recoverable": False,
                    "details": {"payload_type": str(type(payload))},
                },
                "results": None,
            }

        if "state" not in payload or "protocol_version" not in payload:
            if self.strict_v2_enforcement:
                return {
                    "protocol_version": self.protocol_version,
                    "request_id": payload.get("request_id"),
                    "state": "failed",
                    "error": {
                        "code": ERROR_PRECONDITION,
                        "message": (
                            "Legacy bridge response envelope rejected. "
                            "Bridge must emit protocol v2 envelopes."
                        ),
                        "recoverable": False,
                        "details": {"received_keys": sorted(payload.keys())},
                    },
                }
            return {
                "protocol_version": self.protocol_version,
                "request_id": payload.get("request_id"),
                "state": "succeeded",
                "results": payload.get("results"),
            }

        normalized = dict(payload)
        normalized.setdefault("protocol_version", self.protocol_version)
        normalized.setdefault("state", "succeeded")
        return normalized

    def _is_mutating_action(self, action: str | None) -> bool:
        return isinstance(action, str) and action in MUTATING_BRIDGE_ACTIONS

    def _default_timeout_for_action(self, action: str | None) -> float:
        if not isinstance(action, str):
            return 2.0
        if action in BULK_BRIDGE_ACTIONS:
            return 8.0
        if action in MUTATING_BRIDGE_ACTIONS:
            return 5.0
        return 3.0

    def _push_event(
        self,
        *,
        level: str,
        code: str,
        message: str,
        action: str | None = None,
        request_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        self._event_log.append(
            {
                "timestamp": time.time(),
                "level": level,
                "code": code,
                "message": message,
                "action": action,
                "request_id": request_id,
                "details": details or {},
            }
        )

    @staticmethod
    def _redact_sensitive(data: Any) -> Any:
        if isinstance(data, dict):
            redacted: dict[str, Any] = {}
            for key, value in data.items():
                key_l = str(key).lower()
                if key_l in {"auth_token", "token", "authorization"}:
                    redacted[key] = "<redacted>"
                elif key_l == "auth" and isinstance(value, dict):
                    nested = dict(value)
                    if "token" in nested:
                        nested["token"] = "<redacted>"
                    redacted[key] = MaxMSPConnection._redact_sensitive(nested)
                else:
                    redacted[key] = MaxMSPConnection._redact_sensitive(value)
            return redacted
        if isinstance(data, list):
            return [MaxMSPConnection._redact_sensitive(item) for item in data]
        return data

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        idx = (len(ordered) - 1) * pct
        lower = int(idx)
        upper = min(lower + 1, len(ordered) - 1)
        if lower == upper:
            return ordered[lower]
        frac = idx - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * frac

    def _enforce_capabilities(self, action: str | None) -> None:
        if not self.strict_capability_gating or not isinstance(action, str):
            return
        if action in UNGATED_ACTIONS:
            return
        supported = self.capabilities.get("supported_actions")
        if isinstance(supported, list) and supported and action not in supported:
            raise MaxMCPError(
                ERROR_PRECONDITION,
                f"Bridge does not advertise required action '{action}'.",
                hint="Run get_bridge_diagnostics() and verify supported_actions.",
                recoverable=False,
                details={"supported_actions": supported},
            )

        supports_auth = self.capabilities.get("supports_auth")
        if self.auth_token and supports_auth is False:
            raise MaxMCPError(
                ERROR_PRECONDITION,
                "Auth token configured but bridge does not support auth.",
                recoverable=False,
                details={"auth_token_configured": True},
            )

    def _rolling_latency_samples(self) -> list[dict]:
        now = time.time()
        threshold = now - self.alert_window_seconds
        return [
            sample
            for sample in self._latency_samples
            if isinstance(sample, dict)
            and float(sample.get("timestamp", 0.0)) >= threshold
        ]

    def _compute_alerts(self) -> tuple[list[dict], dict]:
        samples = self._rolling_latency_samples()
        durations = [
            float(sample.get("duration_ms", 0.0))
            for sample in samples
            if isinstance(sample, dict)
        ]
        failures = [
            sample
            for sample in samples
            if isinstance(sample, dict)
            and sample.get("state") in {"failed", "timeout"}
        ]
        request_count = len(samples)
        failure_count = len(failures)
        failure_rate = (failure_count / request_count) if request_count else 0.0
        p95_latency_ms = self._percentile(durations, 0.95)
        queue_capacity = max(1, self.mutation_max_queue)
        queue_depth_ratio = self._queued_mutation_requests / queue_capacity

        alerts: list[dict] = []
        if request_count and failure_rate >= self.alert_failure_rate:
            alerts.append(
                {
                    "code": "ALERT_FAILURE_RATE",
                    "severity": (
                        "error"
                        if failure_rate >= max(self.alert_failure_rate * 2, self.alert_failure_rate + 0.20)
                        else "warn"
                    ),
                    "message": "Rolling bridge failure rate exceeded threshold.",
                    "current": round(failure_rate, 4),
                    "threshold": self.alert_failure_rate,
                    "guidance": "Inspect get_bridge_diagnostics() and recent bridge events.",
                }
            )

        if p95_latency_ms is not None and p95_latency_ms >= self.alert_p95_ms:
            alerts.append(
                {
                    "code": "ALERT_P95_LATENCY",
                    "severity": (
                        "error"
                        if p95_latency_ms >= max(self.alert_p95_ms * 2, self.alert_p95_ms + 1000.0)
                        else "warn"
                    ),
                    "message": "Rolling p95 latency exceeded threshold.",
                    "current": round(float(p95_latency_ms), 3),
                    "threshold": self.alert_p95_ms,
                    "guidance": "Inspect queue pressure and increase operation timeouts for large patches.",
                }
            )

        if queue_depth_ratio >= self.alert_queue_depth:
            alerts.append(
                {
                    "code": "ALERT_QUEUE_SATURATION",
                    "severity": "warn",
                    "message": "Mutation queue depth ratio exceeded threshold.",
                    "current": round(queue_depth_ratio, 4),
                    "threshold": self.alert_queue_depth,
                    "guidance": "Reduce concurrent mutation traffic or increase queue/inflight limits.",
                }
            )

        rolling = {
            "window_seconds": self.alert_window_seconds,
            "request_count": request_count,
            "failure_count": failure_count,
            "failure_rate": round(failure_rate, 4),
            "p95_latency_ms": p95_latency_ms,
            "queue_depth_ratio": round(queue_depth_ratio, 4),
        }
        return alerts, rolling

    def emit_metrics_log(self, *, force: bool = False) -> dict | None:
        now = time.time()
        if (
            not force
            and self.last_metrics_log_emit_at is not None
            and now - self.last_metrics_log_emit_at < self.metrics_log_interval_seconds
        ):
            return None

        snapshot = self.metrics_snapshot(include_events=False)
        payload = {
            "type": "maxmcp.bridge.metrics",
            "timestamp": now,
            "endpoint": self.endpoint,
            "connected": bool(self.sio.connected),
            "metrics": snapshot,
        }
        logging.info("MAXMCP_METRICS %s", json.dumps(payload, sort_keys=True))
        for alert in snapshot.get("alerts", []):
            if isinstance(alert, dict):
                self._push_event(
                    level=alert.get("severity", "warn"),
                    code=alert.get("code", "ALERT"),
                    message=alert.get("message", "Bridge alert"),
                    details={
                        "current": alert.get("current"),
                        "threshold": alert.get("threshold"),
                        "guidance": alert.get("guidance"),
                    },
                )
        self.last_metrics_log_emit_at = now
        return payload

    async def _acquire_mutation_slot(self, action: str) -> float:
        token = uuid.uuid4().hex
        queued_before = 0
        now = time.time()
        async with self._mutation_condition:
            queued_before = len(self._mutation_waiters)
            if queued_before >= self.mutation_max_queue:
                self.total_failures += 1
                self.consecutive_failures += 1
                self.last_failed_request_at = now
                self.action_failure_counts[action] += 1
                self.mutation_queue_rejections += 1
                self._push_event(
                    level="warn",
                    code=ERROR_OVERLOADED,
                    message="Mutation queue capacity exceeded.",
                    action=action,
                    details={
                        "queued": queued_before,
                        "inflight": self._inflight_mutation_requests,
                        "max_queue": self.mutation_max_queue,
                        "max_inflight": self.mutation_max_inflight,
                    },
                )
                raise MaxMCPError(
                    ERROR_OVERLOADED,
                    "Mutation queue is full. Retry after in-flight patch operations complete.",
                    hint="Reduce concurrent mutation requests or raise MAXMCP_MUTATION_MAX_QUEUE.",
                    recoverable=True,
                    details={
                        "queued": queued_before,
                        "inflight": self._inflight_mutation_requests,
                    },
                )

            self._mutation_waiters.append(token)
            self._queued_mutation_requests = len(self._mutation_waiters)
            self.max_queue_depth_seen = max(
                self.max_queue_depth_seen,
                self._queued_mutation_requests + self._inflight_mutation_requests,
            )

        queue_start = time.perf_counter()
        deadline = time.monotonic() + self.mutation_queue_wait_timeout_seconds
        while True:
            async with self._mutation_condition:
                remaining = deadline - time.monotonic()
                is_head = bool(self._mutation_waiters) and self._mutation_waiters[0] == token
                can_run = self._inflight_mutation_requests < self.mutation_max_inflight
                if is_head and can_run:
                    self._mutation_waiters.popleft()
                    self._queued_mutation_requests = len(self._mutation_waiters)
                    self._inflight_mutation_requests += 1
                    self.max_queue_depth_seen = max(
                        self.max_queue_depth_seen,
                        self._queued_mutation_requests + self._inflight_mutation_requests,
                    )
                    wait_seconds = max(0.0, time.perf_counter() - queue_start)
                    self.total_mutation_queue_wait_seconds += wait_seconds
                    self._mutation_condition.notify_all()
                    return wait_seconds

                if remaining <= 0:
                    if token in self._mutation_waiters:
                        self._mutation_waiters.remove(token)
                    self._queued_mutation_requests = len(self._mutation_waiters)
                    self.total_failures += 1
                    self.consecutive_failures += 1
                    self.last_failed_request_at = time.time()
                    self.action_failure_counts[action] += 1
                    self.mutation_queue_timeouts += 1
                    self.mutation_queue_rejections += 1
                    self._push_event(
                        level="warn",
                        code=ERROR_OVERLOADED,
                        message="Timed out waiting for mutation queue slot.",
                        action=action,
                        details={
                            "queue_wait_timeout_seconds": self.mutation_queue_wait_timeout_seconds,
                            "queued": self._queued_mutation_requests,
                            "inflight": self._inflight_mutation_requests,
                        },
                    )
                    self._mutation_condition.notify_all()
                    raise MaxMCPError(
                        ERROR_OVERLOADED,
                        (
                            "Timed out waiting for mutation queue slot "
                            f"after {self.mutation_queue_wait_timeout_seconds} seconds."
                        ),
                        hint="Reduce concurrent mutations or raise MAXMCP_MUTATION_QUEUE_WAIT_TIMEOUT_SECONDS.",
                        recoverable=True,
                        details={
                            "queued": self._queued_mutation_requests,
                            "inflight": self._inflight_mutation_requests,
                        },
                    )

                try:
                    await asyncio.wait_for(
                        self._mutation_condition.wait(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    # Loop will process timeout on next iteration.
                    pass

    async def _release_mutation_slot(self) -> None:
        async with self._mutation_condition:
            if self._inflight_mutation_requests > 0:
                self._inflight_mutation_requests -= 1
            self._queued_mutation_requests = len(self._mutation_waiters)
            self._mutation_condition.notify_all()

    def _build_request_envelope(
        self, payload: dict, *, idempotency_key: str | None = None
    ) -> dict:
        request_id = str(uuid.uuid4())
        action = payload.get("action")
        action_payload = payload.get("payload")
        if action_payload is None:
            action_payload = {
                k: v
                for k, v in payload.items()
                if k
                not in {
                    "action",
                    "request_id",
                    "protocol_version",
                    "state",
                    "idempotency_key",
                    "timestamp_ms",
                }
            }

        envelope = {
            "protocol_version": self.protocol_version,
            "request_id": request_id,
            "state": "requested",
            "action": action,
            "payload": action_payload,
            "timestamp_ms": int(time.time() * 1000),
        }
        # Mirror payload fields at top-level for compatibility with legacy Max handlers.
        if isinstance(action_payload, dict):
            for k, v in action_payload.items():
                if k not in envelope:
                    envelope[k] = v
        if idempotency_key:
            envelope["idempotency_key"] = idempotency_key
        return envelope

    def _cache_idempotent_result(self, idempotency_key: str, value: Any) -> None:
        self._idempotent_results[idempotency_key] = value
        self._idempotent_results.move_to_end(idempotency_key)
        while len(self._idempotent_results) > self.max_idempotency_cache_size:
            self._idempotent_results.popitem(last=False)

    def _offline_error_message(self) -> str:
        message = (
            f"MaxMSP bridge unavailable at {self.endpoint}. "
            "Open Max and start the bridge patch containing "
            "`node.script max_mcp_node.js` (default port 5002), then retry."
        )
        if self.last_connect_error:
            message += f" Last connect error: {self.last_connect_error}"
        return message

    async def ensure_connected(self, retries: int = 1, retry_delay: float = 0.5) -> None:
        if self.sio.connected:
            return

        if self.require_handshake_auth and not self.auth_token:
            raise MaxMCPError(
                ERROR_PRECONDITION,
                (
                    "Handshake auth is required but no MAXMCP_AUTH_TOKEN is configured. "
                    "Set MAXMCP_AUTH_TOKEN or MAXMCP_AUTH_TOKEN_FILE."
                ),
                recoverable=False,
                details={
                    "auth_required": True,
                    "auth_token_source": self.auth_token_source,
                    "auth_token_file": str(self.auth_token_file),
                },
            )

        if self.runtime_manager and self.runtime_manager.managed_mode:
            status = await self.runtime_manager.ensure_runtime_ready()
            if status.get("ready"):
                return
            self.last_connect_error = status.get("error")

        for attempt in range(retries + 1):
            connected = await self.start_server()
            if connected:
                return
            if attempt < retries:
                await asyncio.sleep(retry_delay)

        raise MaxMCPError(
            ERROR_BRIDGE_UNAVAILABLE,
            self._offline_error_message(),
            recoverable=True,
        )

    async def send_command(
        self,
        cmd: dict,
        timeout: float = 5.0,
        *,
        idempotency_key: str | None = None,
    ):
        """Send a command and await structured completion."""
        return await self.send_request(
            cmd,
            timeout=timeout,
            idempotency_key=idempotency_key,
        )

    async def send_request(
        self,
        payload: dict,
        timeout: float | None = None,
        *,
        idempotency_key: str | None = None,
        include_envelope: bool = False,
    ):
        """Send a request to MaxMSP and return results (or full envelope)."""
        if not isinstance(payload, dict):
            raise MaxMCPError(
                ERROR_VALIDATION,
                "Bridge request payload must be a dictionary.",
                recoverable=False,
                details={"payload_type": str(type(payload))},
            )
        if not payload.get("action"):
            raise MaxMCPError(
                ERROR_VALIDATION,
                "Bridge request payload must include an 'action'.",
                recoverable=False,
            )

        action = payload.get("action")
        timeout_seconds = (
            float(timeout)
            if timeout is not None
            else self._default_timeout_for_action(action)
        )
        queue_wait_seconds = 0.0
        acquired_mutation_slot = False
        request_id: str | None = None

        if isinstance(action, str):
            self.action_request_counts[action] += 1

        await self.ensure_connected()
        if (
            self.strict_capability_gating
            and isinstance(action, str)
            and action not in UNGATED_ACTIONS
            and not self.capabilities
        ):
            await self.refresh_capabilities()
        self._enforce_capabilities(action)

        if idempotency_key and idempotency_key in self._idempotent_results:
            cached = self._idempotent_results[idempotency_key]
            self._push_event(
                level="info",
                code="IDEMPOTENCY_CACHE_HIT",
                message="Served request from idempotency cache.",
                action=action if isinstance(action, str) else None,
                details={"idempotency_key": idempotency_key},
            )
            if include_envelope:
                return {
                    "protocol_version": self.protocol_version,
                    "request_id": "idempotent-cache-hit",
                    "state": "succeeded",
                    "results": cached,
                    "meta": {"idempotency_cache_hit": True},
                }
            return cached

        if self._is_mutating_action(action):
            queue_wait_seconds = await self._acquire_mutation_slot(action)
            acquired_mutation_slot = True

        envelope = self._build_request_envelope(payload, idempotency_key=idempotency_key)
        request_id = envelope["request_id"]
        if self.auth_token:
            envelope["auth_token"] = self.auth_token
            envelope["auth"] = {"token": self.auth_token}
        future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        self.total_requests += 1
        self.last_request_at = time.time()
        started_at = time.perf_counter()

        try:
            await self.sio.emit("request", envelope, namespace=self.namespace)
            logging.info("Request to MaxMSP: %s", self._redact_sensitive(envelope))
            response_envelope = await asyncio.wait_for(future, timeout_seconds)
            self.last_response_at = time.time()
            state = response_envelope.get("state", "succeeded")
            duration_ms = (time.perf_counter() - started_at) * 1000.0
            if state == "failed":
                self.total_failures += 1
                self.consecutive_failures += 1
                self.last_failed_request_at = time.time()
                if isinstance(action, str):
                    self.action_failure_counts[action] += 1
                err = response_envelope.get("error") or {}
                err_code = err.get("code", ERROR_INTERNAL)
                self._latency_samples.append(
                    {
                        "duration_ms": round(duration_ms, 3),
                        "queue_wait_ms": round(queue_wait_seconds * 1000.0, 3),
                        "timestamp": time.time(),
                        "action": action,
                        "state": "failed",
                        "code": err_code,
                    }
                )
                self._push_event(
                    level="error",
                    code=err_code,
                    message=err.get("message", "Bridge request failed."),
                    action=action if isinstance(action, str) else None,
                    request_id=request_id,
                    details=err.get("details") if isinstance(err.get("details"), dict) else {},
                )
                raise MaxMCPError(
                    err_code,
                    err.get("message", "Bridge request failed."),
                    hint=err.get("hint"),
                    recoverable=bool(err.get("recoverable", True)),
                    details=err.get("details") if isinstance(err.get("details"), dict) else {},
                )

            self.total_successes += 1
            self.consecutive_failures = 0
            self.last_successful_request_at = time.time()
            self._latency_samples.append(
                {
                    "duration_ms": round(duration_ms, 3),
                    "queue_wait_ms": round(queue_wait_seconds * 1000.0, 3),
                    "timestamp": time.time(),
                    "action": action,
                    "state": "succeeded",
                }
            )
            results = response_envelope.get("results")
            if idempotency_key:
                self._cache_idempotent_result(idempotency_key, results)
            if self.runtime_manager and isinstance(action, str):
                try:
                    await self.runtime_manager.after_successful_action(
                        action,
                        payload,
                        results,
                    )
                except Exception as e:
                    logging.warning(f"Post-action twin sync failed for '{action}': {e}")
            if include_envelope:
                return response_envelope
            return results
        except asyncio.TimeoutError:
            self.total_timeouts += 1
            self.total_failures += 1
            self.consecutive_failures += 1
            self.last_timeout_at = time.time()
            self.last_failed_request_at = self.last_timeout_at
            duration_ms = (time.perf_counter() - started_at) * 1000.0
            if isinstance(action, str):
                self.action_timeout_counts[action] += 1
                self.action_failure_counts[action] += 1
            self._latency_samples.append(
                {
                    "duration_ms": round(duration_ms, 3),
                    "queue_wait_ms": round(queue_wait_seconds * 1000.0, 3),
                    "timestamp": time.time(),
                    "action": action,
                    "state": "timeout",
                    "code": ERROR_BRIDGE_TIMEOUT,
                }
            )
            self._push_event(
                level="warn",
                code=ERROR_BRIDGE_TIMEOUT,
                message=f"No response received in {timeout_seconds} seconds.",
                action=action if isinstance(action, str) else None,
                request_id=request_id,
            )
            raise MaxMCPError(
                ERROR_BRIDGE_TIMEOUT,
                f"No response received in {timeout_seconds} seconds.",
                hint="Check bridge health and retry with a higher timeout for large patches.",
                recoverable=True,
            )
        except MaxMCPError:
            raise
        except Exception as e:
            self.total_failures += 1
            self.consecutive_failures += 1
            self.last_failed_request_at = time.time()
            if isinstance(action, str):
                self.action_failure_counts[action] += 1
            self._push_event(
                level="error",
                code=ERROR_INTERNAL,
                message=f"Bridge request transport failure: {e}",
                action=action if isinstance(action, str) else None,
                request_id=request_id,
            )
            raise MaxMCPError(
                ERROR_INTERNAL,
                f"Bridge request transport failure: {e}",
                hint="Verify bridge connectivity and retry.",
                recoverable=True,
            )
        finally:
            self._pending.pop(request_id, None)
            if acquired_mutation_slot:
                await self._release_mutation_slot()

    async def start_server(self) -> bool:
        """IMPORTANT: This method should only be called ONCE per application instance.
        Multiple calls can lead to binding multiple ports unnecessarily.
        """
        if self.sio.connected:
            return True

        try:
            # Connect to the server
            full_url = f"{self.server_url}:{self.server_port}"
            connect_kwargs: dict[str, Any] = {"namespaces": [self.namespace]}
            if self.auth_token:
                connect_kwargs["auth"] = {"token": self.auth_token}
                connect_kwargs["headers"] = {"x-maxmcp-token": self.auth_token}
            await self.sio.connect(full_url, **connect_kwargs)
            self.last_connect_error = None
            self.connected_at = time.time()
            logging.info(f"Connected to Socket.IO server at {full_url}")
            await self.refresh_capabilities()
            return True

        except Exception as e:
            self.last_connect_error = str(e)
            logging.warning(
                f"MaxMSP Socket.IO bridge not reachable at {self.endpoint}: {e}"
            )
            return False

    async def disconnect(self) -> None:
        if self.sio.connected:
            await self.sio.disconnect()

    async def refresh_capabilities(self) -> dict:
        try:
            caps = await self.send_request({"action": "capabilities"}, timeout=2.0)
            if isinstance(caps, dict):
                caps.setdefault("maxpy_catalog", {})
                if isinstance(caps["maxpy_catalog"], dict):
                    caps["maxpy_catalog"].update(
                        {
                            "available": maxpy_catalog.available,
                            "schema_hash": maxpy_catalog.schema_hash,
                            "object_count": maxpy_catalog.count,
                            "packages": maxpy_catalog.packages,
                        }
                    )
                self.capabilities = caps
                return caps
        except Exception as e:
            self.last_connect_error = str(e)
        return {}

    async def ping_bridge(self, timeout: float = 2.0) -> dict:
        try:
            ping = await self.send_request({"action": "health_ping"}, timeout=timeout)
            self.last_heartbeat_at = time.time()
            self.last_heartbeat_error = None
            return {"ok": True, "response": ping}
        except Exception as e:
            self.last_heartbeat_error = str(e)
            return {"ok": False, "error": str(e)}

    def health_snapshot(self) -> dict:
        now = time.time()
        response_age = None
        if self.last_response_at is not None:
            response_age = round(now - self.last_response_at, 3)
        request_age = None
        if self.last_request_at is not None:
            request_age = round(now - self.last_request_at, 3)
        stale = response_age is None or response_age > self.stale_threshold_seconds

        return {
            "protocol_version": self.protocol_version,
            "connected": bool(self.sio.connected),
            "endpoint": self.endpoint,
            "stale": stale,
            "response_age_seconds": response_age,
            "request_age_seconds": request_age,
            "stale_threshold_seconds": self.stale_threshold_seconds,
            "connected_at": self.connected_at,
            "last_request_at": self.last_request_at,
            "last_response_at": self.last_response_at,
            "last_successful_request_at": self.last_successful_request_at,
            "last_failed_request_at": self.last_failed_request_at,
            "last_timeout_at": self.last_timeout_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_heartbeat_error": self.last_heartbeat_error,
            "total_requests": self.total_requests,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "total_timeouts": self.total_timeouts,
            "consecutive_failures": self.consecutive_failures,
            "last_connect_error": self.last_connect_error,
            "mutation_inflight": self._inflight_mutation_requests,
            "mutation_queued": self._queued_mutation_requests,
            "mutation_queue_rejections": self.mutation_queue_rejections,
            "mutation_queue_timeouts": self.mutation_queue_timeouts,
            "max_queue_depth_seen": self.max_queue_depth_seen,
            "auth": {
                "required": self.require_handshake_auth,
                "configured": bool(self.auth_token),
                "source": self.auth_token_source,
                "token_file": str(self.auth_token_file),
            },
            "capabilities": self.capabilities,
        }

    def metrics_snapshot(
        self,
        *,
        include_events: bool = False,
        event_limit: int = 25,
    ) -> dict:
        latency_samples = list(self._latency_samples)
        durations = [
            float(sample.get("duration_ms", 0.0))
            for sample in latency_samples
            if isinstance(sample, dict)
        ]
        queue_waits = [
            float(sample.get("queue_wait_ms", 0.0))
            for sample in latency_samples
            if isinstance(sample, dict)
        ]

        action_stats = {}
        for action_name in sorted(self.action_request_counts.keys()):
            total = self.action_request_counts[action_name]
            failed = self.action_failure_counts.get(action_name, 0)
            timeouts = self.action_timeout_counts.get(action_name, 0)
            succeeded = max(0, total - failed)
            action_stats[action_name] = {
                "requests": total,
                "succeeded": succeeded,
                "failed": failed,
                "timeouts": timeouts,
            }

        snapshot = {
            "protocol_version": self.protocol_version,
            "total_requests": self.total_requests,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "total_timeouts": self.total_timeouts,
            "latency_samples": len(latency_samples),
            "latency_ms": {
                "p50": self._percentile(durations, 0.50),
                "p95": self._percentile(durations, 0.95),
                "p99": self._percentile(durations, 0.99),
                "max": max(durations) if durations else None,
            },
            "queue_wait_ms": {
                "p50": self._percentile(queue_waits, 0.50),
                "p95": self._percentile(queue_waits, 0.95),
                "p99": self._percentile(queue_waits, 0.99),
                "max": max(queue_waits) if queue_waits else None,
                "total_seconds": round(self.total_mutation_queue_wait_seconds, 6),
            },
            "mutation_queue": {
                "inflight": self._inflight_mutation_requests,
                "queued": self._queued_mutation_requests,
                "max_inflight": self.mutation_max_inflight,
                "max_queue": self.mutation_max_queue,
                "max_depth_seen": self.max_queue_depth_seen,
                "rejections": self.mutation_queue_rejections,
                "timeouts": self.mutation_queue_timeouts,
            },
            "actions": action_stats,
            "last_log_emit_at": self.last_metrics_log_emit_at,
        }
        alerts, rolling = self._compute_alerts()
        snapshot["alerts"] = alerts
        snapshot["rolling_windows"] = rolling
        if include_events:
            bounded = max(1, int(event_limit))
            snapshot["recent_events"] = list(self._event_log)[-bounded:]
        return snapshot


class MaxRuntimeManager:
    """Managed runtime layer that keeps Max and the bridge patch available."""

    def __init__(self, maxmsp: MaxMSPConnection):
        self.maxmsp = maxmsp
        self.managed_mode = MAXMCP_MANAGED_MODE
        self.state_dir = MAXMCP_STATE_DIR
        self.state_file = MAXMCP_STATE_FILE
        self.max_app_path = MAX_APP_PATH
        self.host_patch_path = HOST_PATCH_PATH
        self.fallback_patch_path = FALLBACK_PATCH_PATH
        self.npm_project_dir = MAXMCP_NPM_PROJECT_DIR
        self.npm_sentinel = MAXMCP_NPM_SENTINEL
        self.npm_auto_install = MAXMCP_NPM_AUTO_INSTALL
        self.sessions_root = MAXMCP_SESSIONS_ROOT
        self.session_id = MAXMCP_SESSION_ID
        self.session_dir = self.sessions_root / self.session_id
        self.enforce_patch_roots = MAXMCP_ENFORCE_PATCH_ROOTS
        self.session_active_patch = self.session_dir / "active.maxpat"
        self.session_scratch_patch = self.session_dir / "scratch.maxpat"
        configured_roots = list(MAXMCP_ALLOWED_PATCH_ROOTS)
        if self.enforce_patch_roots and not configured_roots:
            configured_roots = [Path(current_dir).resolve(), self.session_dir.resolve()]
        self.allowed_patch_roots = configured_roots
        self.workspace_active_varname = (
            f"{PROTECTED_VARNAME_PREFIX}workspace_active_{self.session_id}"
        )
        self.workspace_scratch_varname = (
            f"{PROTECTED_VARNAME_PREFIX}workspace_scratch_{self.session_id}"
        )
        self.active_target = "active"
        self.checkpoints: OrderedDict[str, dict] = OrderedDict()
        self.checkpoints_file = self.session_dir / "checkpoints.json"
        self.max_checkpoints = MAXMCP_CHECKPOINT_MAX
        self.twin_auto_sync = MAXMCP_TWIN_AUTO_SYNC
        self.twin_baseline_hash: str | None = None
        self.twin_last_live_hash: str | None = None
        self.twin_last_sync_at: float | None = None
        self.twin_last_check_at: float | None = None
        self.twin_last_error: str | None = None
        self.twin_last_reason: str | None = None
        self.twin_object_count = 0
        self.twin_connection_count = 0
        self.twin_live_object_count = 0
        self.twin_live_connection_count = 0
        self.twin_last_drift: bool | None = None
        self.hygiene_manager = None
        self._lock = asyncio.Lock()
        self._last_launch_at = 0.0
        self._launch_cooldown_seconds = 1.5

    @staticmethod
    def _canonical_topology(topology: dict) -> dict:
        boxes = []
        lines = []
        if isinstance(topology, dict):
            for item in topology.get("boxes", []):
                if not isinstance(item, dict):
                    continue
                box = item.get("box", {})
                if not isinstance(box, dict):
                    continue
                boxes.append(
                    {
                        "varname": box.get("varname"),
                        "maxclass": box.get("maxclass"),
                        "patching_rect": box.get("patching_rect"),
                        "numinlets": box.get("numinlets"),
                        "numoutlets": box.get("numoutlets"),
                        "boxtext": box.get("boxtext"),
                        "attributes": box.get("attributes"),
                    }
                )
            for item in topology.get("lines", []):
                if not isinstance(item, dict):
                    continue
                line = item.get("patchline", {})
                if not isinstance(line, dict):
                    continue
                lines.append(
                    {
                        "source": line.get("source"),
                        "destination": line.get("destination"),
                    }
                )
        boxes.sort(
            key=lambda box: (
                str(box.get("varname") or ""),
                str(box.get("maxclass") or ""),
                json.dumps(box.get("patching_rect"), sort_keys=True, default=str),
                str(box.get("boxtext") or ""),
                json.dumps(box.get("attributes"), sort_keys=True, default=str),
            )
        )
        lines.sort(
            key=lambda line: (
                json.dumps(line.get("source"), sort_keys=True, default=str),
                json.dumps(line.get("destination"), sort_keys=True, default=str),
            )
        )
        return {"boxes": boxes, "lines": lines}

    @classmethod
    def _topology_hash(cls, topology: dict) -> tuple[str, int, int]:
        canonical = cls._canonical_topology(topology)
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return digest, len(canonical["boxes"]), len(canonical["lines"])

    def _twin_status_payload(self) -> dict:
        return {
            "baseline_hash": self.twin_baseline_hash,
            "last_live_hash": self.twin_last_live_hash,
            "last_sync_at": self.twin_last_sync_at,
            "last_check_at": self.twin_last_check_at,
            "last_error": self.twin_last_error,
            "last_reason": self.twin_last_reason,
            "baseline_object_count": self.twin_object_count,
            "baseline_connection_count": self.twin_connection_count,
            "live_object_count": self.twin_live_object_count,
            "live_connection_count": self.twin_live_connection_count,
            "last_drift": self.twin_last_drift,
            "auto_sync_enabled": self.twin_auto_sync,
        }

    async def _capture_live_topology(self) -> dict:
        topology = await self.maxmsp.send_request(
            {"action": "get_objects_in_patch"},
            timeout=8.0,
        )
        if not isinstance(topology, dict):
            raise MaxMCPError(
                ERROR_INTERNAL,
                "Bridge returned unexpected topology payload.",
                recoverable=True,
                details={"payload_type": str(type(topology))},
            )
        return topology

    async def sync_patch_twin(self, reason: str = "manual") -> dict:
        if not self.maxmsp.sio.connected:
            return {
                "success": False,
                "error": "bridge_disconnected",
                "twin": self._twin_status_payload(),
            }
        try:
            topology = await self._capture_live_topology()
            digest, object_count, connection_count = self._topology_hash(topology)
            now = time.time()
            self.twin_baseline_hash = digest
            self.twin_last_live_hash = digest
            self.twin_last_sync_at = now
            self.twin_last_check_at = now
            self.twin_last_error = None
            self.twin_last_reason = reason
            self.twin_object_count = object_count
            self.twin_connection_count = connection_count
            self.twin_live_object_count = object_count
            self.twin_live_connection_count = connection_count
            self.twin_last_drift = False
            return {
                "success": True,
                "in_sync": True,
                "reason": reason,
                "hash": digest,
                "object_count": object_count,
                "connection_count": connection_count,
                "twin": self._twin_status_payload(),
            }
        except Exception as e:
            self.twin_last_error = str(e)
            return {
                "success": False,
                "error": str(e),
                "reason": reason,
                "twin": self._twin_status_payload(),
            }

    async def check_patch_drift(self, auto_resync: bool = False) -> dict:
        if not self.maxmsp.sio.connected:
            return {
                "success": False,
                "error": "bridge_disconnected",
                "twin": self._twin_status_payload(),
            }

        try:
            topology = await self._capture_live_topology()
            live_hash, live_objects, live_connections = self._topology_hash(topology)
            now = time.time()
            self.twin_last_live_hash = live_hash
            self.twin_last_check_at = now
            self.twin_live_object_count = live_objects
            self.twin_live_connection_count = live_connections
            self.twin_last_error = None

            if self.twin_baseline_hash is None:
                self.twin_baseline_hash = live_hash
                self.twin_last_sync_at = now
                self.twin_object_count = live_objects
                self.twin_connection_count = live_connections
                self.twin_last_drift = False
                return {
                    "success": True,
                    "in_sync": True,
                    "initialized": True,
                    "twin": self._twin_status_payload(),
                }

            in_sync = self.twin_baseline_hash == live_hash
            self.twin_last_drift = not in_sync
            response = {
                "success": True,
                "in_sync": in_sync,
                "baseline_hash": self.twin_baseline_hash,
                "live_hash": live_hash,
                "baseline_object_count": self.twin_object_count,
                "baseline_connection_count": self.twin_connection_count,
                "live_object_count": live_objects,
                "live_connection_count": live_connections,
                "twin": self._twin_status_payload(),
            }
            if not in_sync and auto_resync:
                sync_result = await self.sync_patch_twin(reason="auto_resync_after_drift")
                response["auto_resync"] = sync_result
            return response
        except Exception as e:
            self.twin_last_error = str(e)
            return {"success": False, "error": str(e), "twin": self._twin_status_payload()}

    def _host_mutation_error(self, operation: str) -> dict:
        return {
            "success": False,
            "code": ERROR_PRECONDITION,
            "error": (
                f"{operation} is blocked while target='host'. "
                "Switch to 'active' or 'scratch' before mutating topology."
            ),
            "hint": "Use set_patch_target(target='active') or set_patch_target(target='scratch').",
            "target": self.active_target,
        }

    def _operation_error(
        self,
        *,
        operation: str,
        action: str | None,
        error: Exception | MaxMCPError,
        details: dict | None = None,
    ) -> dict:
        if isinstance(error, MaxMCPError):
            payload = error.to_dict()
        else:
            payload = {
                "code": ERROR_INTERNAL,
                "message": str(error),
                "recoverable": True,
                "details": {},
            }

        merged_details = {}
        if isinstance(payload.get("details"), dict):
            merged_details.update(payload["details"])
        if isinstance(details, dict):
            merged_details.update(details)
        if action:
            merged_details.setdefault("action", action)
        merged_details.setdefault("operation", operation)
        merged_details.setdefault("endpoint", getattr(self.maxmsp, "endpoint", None))
        try:
            metrics = self.maxmsp.metrics_snapshot(include_events=False)
            if isinstance(metrics, dict) and isinstance(metrics.get("mutation_queue"), dict):
                merged_details.setdefault("mutation_queue", metrics["mutation_queue"])
        except Exception:
            pass

        payload["details"] = merged_details
        if payload.get("code") == ERROR_OVERLOADED and not payload.get("hint"):
            payload["hint"] = (
                "Bridge mutation queue is saturated. Retry after in-flight operations complete "
                "or raise MAXMCP_MUTATION_MAX_QUEUE/MAXMCP_MUTATION_MAX_INFLIGHT."
            )
        if payload.get("code") == ERROR_UNAUTHORIZED and not payload.get("hint"):
            payload["hint"] = (
                "Verify MAXMCP_AUTH_TOKEN matches bridge configuration and retry."
            )
        if payload.get("code") == ERROR_PRECONDITION and not payload.get("hint"):
            payload["hint"] = (
                "Run get_bridge_diagnostics() to verify bridge capabilities and runtime prerequisites."
            )
        return {"success": False, "error": payload}

    def _check_required_capabilities(
        self,
        *,
        required_actions: set[str],
        operation: str,
    ) -> dict | None:
        strict = bool(getattr(self.maxmsp, "strict_capability_gating", False))
        if not strict:
            return None
        capabilities = getattr(self.maxmsp, "capabilities", {})
        supported = capabilities.get("supported_actions") if isinstance(capabilities, dict) else None
        if not isinstance(supported, list) or not supported:
            return None

        missing = sorted(action for action in required_actions if action not in supported)
        if not missing:
            return None
        return {
            "success": False,
            "error": {
                "code": ERROR_PRECONDITION,
                "message": (
                    f"Bridge does not advertise required actions for {operation}: {missing}"
                ),
                "hint": "Run get_bridge_diagnostics() and verify supported_actions.",
                "recoverable": False,
                "details": {
                    "operation": operation,
                    "missing_actions": missing,
                    "supported_actions": supported,
                },
            },
        }

    def _checkpoint_entry_summary(self, entry: dict) -> dict:
        return {
            "checkpoint_id": entry.get("checkpoint_id"),
            "label": entry.get("label", ""),
            "created_at": entry.get("created_at"),
            "hash": entry.get("hash"),
            "object_count": entry.get("object_count"),
            "connection_count": entry.get("connection_count"),
            "target": entry.get("target"),
        }

    def _save_checkpoint_journal_sync(self) -> dict:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": self.session_id,
            "updated_at": int(time.time()),
            "checkpoints": [json.loads(json.dumps(entry)) for entry in self.checkpoints.values()],
        }
        self.checkpoints_file.write_text(json.dumps(payload, indent=2))
        return {
            "saved": True,
            "path": str(self.checkpoints_file),
            "count": len(self.checkpoints),
        }

    def _load_checkpoint_journal_sync(self) -> dict:
        if not self.checkpoints_file.exists():
            return {
                "loaded": False,
                "path": str(self.checkpoints_file),
                "count": 0,
            }

        try:
            payload = json.loads(self.checkpoints_file.read_text())
        except Exception as e:
            logging.warning(f"Failed to load checkpoint journal: {e}")
            return {
                "loaded": False,
                "path": str(self.checkpoints_file),
                "count": 0,
                "error": str(e),
            }

        rows = payload.get("checkpoints", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []

        loaded = OrderedDict()
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            checkpoint_id = raw.get("checkpoint_id")
            raw_topology = raw.get("topology")
            if not isinstance(raw_topology, dict):
                continue
            topology = self._extract_topology_from_payload(raw_topology)
            if not isinstance(checkpoint_id, str) or not checkpoint_id:
                continue

            digest, object_count, connection_count = self._topology_hash(topology)
            entry = {
                "checkpoint_id": checkpoint_id,
                "label": str(raw.get("label", "")),
                "created_at": raw.get("created_at", time.time()),
                "hash": raw.get("hash") or digest,
                "object_count": raw.get("object_count", object_count),
                "connection_count": raw.get("connection_count", connection_count),
                "target": raw.get("target", "active"),
                "context": raw.get("context") if isinstance(raw.get("context"), dict) else {},
                "topology": json.loads(json.dumps(topology)),
            }
            loaded[checkpoint_id] = entry
            while len(loaded) > self.max_checkpoints:
                loaded.popitem(last=False)

        self.checkpoints = loaded
        return {
            "loaded": True,
            "path": str(self.checkpoints_file),
            "count": len(self.checkpoints),
        }

    async def create_checkpoint(self, label: str = "") -> dict:
        if self.active_target == "host":
            return _error_result(
                ERROR_PRECONDITION,
                "create_checkpoint is blocked while target='host'.",
                hint="Switch to 'active' or 'scratch' before creating checkpoints.",
                recoverable=False,
                details={"target": self.active_target},
            )
        if not self.maxmsp.sio.connected:
            return _error_result(
                ERROR_BRIDGE_UNAVAILABLE,
                "Bridge is disconnected; cannot create checkpoint.",
                recoverable=True,
                details={"target": self.active_target},
            )
        try:
            topology = await self._capture_live_topology()
        except Exception as e:
            return self._operation_error(
                operation="create_checkpoint",
                action="get_objects_in_patch",
                error=e,
                details={"target": self.active_target},
            )
        try:
            context = await self.maxmsp.send_request(
                {"action": "get_patcher_context"},
                timeout=3.0,
            )
        except Exception as e:
            return self._operation_error(
                operation="create_checkpoint",
                action="get_patcher_context",
                error=e,
                details={"target": self.active_target},
            )

        try:
            digest, object_count, connection_count = self._topology_hash(topology)
            checkpoint_id = uuid.uuid4().hex[:10]
            entry = {
                "checkpoint_id": checkpoint_id,
                "label": label or "",
                "created_at": time.time(),
                "hash": digest,
                "object_count": object_count,
                "connection_count": connection_count,
                "target": self.active_target,
                "context": context if isinstance(context, dict) else {},
                "topology": json.loads(json.dumps(topology)),
            }
            self.checkpoints[checkpoint_id] = entry
            self.checkpoints.move_to_end(checkpoint_id)
            while len(self.checkpoints) > self.max_checkpoints:
                self.checkpoints.popitem(last=False)
            try:
                checkpoint_journal = await asyncio.to_thread(self._save_checkpoint_journal_sync)
            except Exception as e:
                return self._operation_error(
                    operation="create_checkpoint",
                    action="checkpoint_journal_write",
                    error=e,
                    details={"checkpoint_id": checkpoint_id},
                )
            return {
                "success": True,
                "checkpoint_id": checkpoint_id,
                "label": entry["label"],
                "created_at": entry["created_at"],
                "hash": digest,
                "object_count": object_count,
                "connection_count": connection_count,
                "target": self.active_target,
                "total_checkpoints": len(self.checkpoints),
                "checkpoint_journal": checkpoint_journal,
            }
        except Exception as e:
            return self._operation_error(
                operation="create_checkpoint",
                action="capture_checkpoint_state",
                error=e,
                details={"target": self.active_target},
            )

    def list_checkpoints(self) -> list[dict]:
        return [
            self._checkpoint_entry_summary(entry)
            for entry in reversed(list(self.checkpoints.values()))
        ]

    async def restore_checkpoint(self, checkpoint_id: str) -> dict:
        if self.active_target == "host":
            return _error_result(
                ERROR_PRECONDITION,
                "restore_checkpoint is blocked while target='host'.",
                hint="Switch to 'active' or 'scratch' before restoring checkpoints.",
                recoverable=False,
                details={"target": self.active_target, "checkpoint_id": checkpoint_id},
            )
        entry = self.checkpoints.get(checkpoint_id)
        if entry is None:
            return _error_result(
                ERROR_OBJECT_NOT_FOUND,
                f"Checkpoint not found: {checkpoint_id}",
                recoverable=True,
                details={"checkpoint_id": checkpoint_id},
            )
        if not self.maxmsp.sio.connected:
            return _error_result(
                ERROR_BRIDGE_UNAVAILABLE,
                "Bridge is disconnected; cannot restore checkpoint.",
                recoverable=True,
                details={"checkpoint_id": checkpoint_id},
            )
        topology = entry.get("topology")
        if not isinstance(topology, dict):
            return _error_result(
                ERROR_PRECONDITION,
                "Checkpoint topology is missing or invalid.",
                recoverable=False,
                details={"checkpoint_id": checkpoint_id},
            )
        try:
            result = await self.maxmsp.send_request(
                {"action": "apply_topology_snapshot", "snapshot": topology},
                timeout=20.0,
            )
            twin = await self.sync_patch_twin(reason=f"restore_checkpoint:{checkpoint_id}")
            return {
                "success": True,
                "checkpoint_id": checkpoint_id,
                "applied": result,
                "twin": twin,
            }
        except Exception as e:
            return self._operation_error(
                operation="restore_checkpoint",
                action="apply_topology_snapshot",
                error=e,
                details={"checkpoint_id": checkpoint_id},
            )

    async def after_successful_action(self, action: str, payload: dict, results: Any) -> None:
        topology_mutations = {
            "add_object",
            "remove_object",
            "connect_objects",
            "disconnect_objects",
            "create_subpatcher",
            "add_subpatcher_io",
            "recreate_with_args",
            "move_object",
            "encapsulate",
            "apply_topology_snapshot",
            "set_workspace_target",
        }
        workspace_persist_actions = (topology_mutations - {"set_workspace_target"}) | {
            "set_object_attribute",
            "set_message_text",
            "send_message_to_object",
            "set_number",
            "send_bang_to_object",
            "autofit_existing",
        }
        if action in topology_mutations and self.twin_auto_sync:
            await self.sync_patch_twin(reason=f"mutation:{action}")
        if action in workspace_persist_actions and self.active_target in {"active", "scratch"}:
            persist_result = await self.persist_workspace_target(reason=f"mutation:{action}")
            if not persist_result.get("persisted"):
                logging.debug(
                    "Workspace persistence skipped after action '%s': %s",
                    action,
                    persist_result,
                )

    def _resolve_host_patch(self) -> Path | None:
        if self.host_patch_path.exists():
            return self.host_patch_path
        if self.fallback_patch_path.exists():
            return self.fallback_patch_path
        return None

    def _ensure_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _patch_template_payload(self) -> dict:
        if MAXPYLANG_TEMPLATE_PATH.exists():
            try:
                payload = json.loads(MAXPYLANG_TEMPLATE_PATH.read_text())
                if isinstance(payload, dict):
                    return payload
            except Exception:
                pass

        # Minimal fallback patch if MaxPyLang template is unavailable.
        return {
            "patcher": {
                "fileversion": 1,
                "appversion": {"major": 9, "minor": 0, "revision": 0, "architecture": "x64"},
                "classnamespace": "box",
                "rect": [0.0, 0.0, 960.0, 720.0],
                "bglocked": 0,
                "openinpresentation": 0,
                "default_fontsize": 12.0,
                "default_fontface": 0,
                "default_fontname": "Arial",
                "gridonopen": 1,
                "gridsize": [15.0, 15.0],
                "gridsnaponopen": 1,
                "statusbarvisible": 2,
                "toolbarvisible": 1,
                "boxes": [],
                "lines": [],
            }
        }

    def _ensure_session_patches_sync(self) -> dict:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        template = self._patch_template_payload()
        created: list[str] = []
        for patch_path in (self.session_active_patch, self.session_scratch_patch):
            if patch_path.exists():
                continue
            patch_path.write_text(json.dumps(template, indent=2))
            created.append(str(patch_path))
        return {
            "session_id": self.session_id,
            "session_dir": str(self.session_dir),
            "created": created,
            "active_patch_path": str(self.session_active_patch),
            "scratch_patch_path": str(self.session_scratch_patch),
        }

    def _workspace_path_for_target(self, target: str) -> Path | None:
        if target == "active":
            return self.session_active_patch
        if target == "scratch":
            return self.session_scratch_patch
        return None

    @staticmethod
    def _path_within_root(path: Path, root: Path) -> bool:
        try:
            return path == root or root in path.parents
        except Exception:
            return False

    def _validate_patch_path_policy(self, path: Path, *, purpose: str) -> None:
        if not self.enforce_patch_roots:
            return
        allowed = self.allowed_patch_roots
        if not allowed:
            return
        if any(self._path_within_root(path, root) for root in allowed):
            return
        raise MaxMCPError(
            ERROR_PRECONDITION,
            f"Path is outside allowed roots for {purpose}: {path}",
            hint=(
                "Set MAXMCP_ALLOWED_PATCH_ROOTS to include this location or disable enforcement "
                "with MAXMCP_ENFORCE_PATCH_ROOTS=0."
            ),
            recoverable=False,
            details={
                "purpose": purpose,
                "path": str(path),
                "allowed_roots": [str(root) for root in allowed],
            },
        )

    def _resolve_patch_path(self, path: str) -> Path:
        raw = (path or "").strip()
        if not raw:
            raise MaxMCPError(
                ERROR_VALIDATION,
                "Patch path must be a non-empty string.",
                recoverable=False,
            )
        resolved = Path(raw).expanduser()
        if not resolved.is_absolute():
            resolved = (Path.cwd() / resolved).resolve()
        else:
            resolved = resolved.resolve()
        if not resolved.exists():
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Patch path does not exist: {resolved}",
                recoverable=True,
            )
        if not resolved.is_file():
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Patch path is not a file: {resolved}",
                recoverable=True,
            )
        if resolved.suffix.lower() not in {".maxpat", ".json"}:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Unsupported file extension '{resolved.suffix}'. Expected .maxpat or .json.",
                recoverable=True,
            )
        self._validate_patch_path_policy(resolved, purpose="patch_read")
        return resolved

    @staticmethod
    def _extract_topology_with_format(payload: Any) -> tuple[str, dict] | None:
        if not isinstance(payload, dict):
            return None

        if isinstance(payload.get("boxes"), list) and isinstance(payload.get("lines"), list):
            return (
                "topology",
                {
                    "boxes": json.loads(json.dumps(payload.get("boxes", []))),
                    "lines": json.loads(json.dumps(payload.get("lines", []))),
                },
            )

        patcher = payload.get("patcher")
        if isinstance(patcher, dict):
            if isinstance(patcher.get("boxes"), list) and isinstance(patcher.get("lines"), list):
                return (
                    "maxpat_patcher",
                    {
                        "boxes": json.loads(json.dumps(patcher.get("boxes", []))),
                        "lines": json.loads(json.dumps(patcher.get("lines", []))),
                    },
                )
        return None

    @staticmethod
    def _extract_topology_from_payload(payload: Any) -> dict:
        extracted = MaxRuntimeManager._extract_topology_with_format(payload)
        if extracted:
            return extracted[1]
        return {"boxes": [], "lines": []}

    def _load_patch_topology_sync(self, source_path: Path) -> dict:
        try:
            payload = json.loads(source_path.read_text())
        except Exception as e:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Failed to parse JSON at {source_path}: {e}",
                recoverable=True,
            ) from e

        extracted = self._extract_topology_with_format(payload)
        if extracted is None:
            raise MaxMCPError(
                ERROR_VALIDATION,
                (
                    f"File {source_path} is not a supported patch payload. "
                    "Expected either top-level boxes/lines or patcher.boxes/patcher.lines."
                ),
                recoverable=True,
            )
        detected_format, topology = extracted
        digest, object_count, connection_count = self._topology_hash(topology)
        return {
            "format": detected_format,
            "topology": topology,
            "hash": digest,
            "object_count": object_count,
            "connection_count": connection_count,
        }

    @staticmethod
    def _topology_varnames(topology: dict) -> set[str]:
        names: set[str] = set()
        for row in topology.get("boxes", []):
            if not isinstance(row, dict):
                continue
            box = row.get("box", row)
            if not isinstance(box, dict):
                continue
            varname = box.get("varname")
            if isinstance(varname, str) and varname:
                names.add(varname)
        return names

    @staticmethod
    def _is_topology_empty(topology: dict) -> bool:
        return len(topology.get("boxes", [])) == 0 and len(topology.get("lines", [])) == 0

    @staticmethod
    def _generate_unique_varname(base: str, used: set[str]) -> str:
        safe_base = base if base else "imp_obj"
        candidate = safe_base
        suffix = 1
        while candidate in used:
            candidate = f"{safe_base}__imp{suffix}"
            suffix += 1
        return candidate

    @classmethod
    def _normalize_import_topology(
        cls,
        topology: dict,
        *,
        reserved_varnames: set[str] | None = None,
        auto_rename_collisions: bool = True,
    ) -> dict:
        reserved = set(reserved_varnames or set())
        used = set(reserved)
        seen_source: set[str] = set()
        remap: dict[str, str] = {}
        collisions = 0
        generated_varnames = 0
        normalized_boxes: list[dict] = []

        for idx, row in enumerate(topology.get("boxes", [])):
            if not isinstance(row, dict):
                continue
            box = row.get("box", row)
            if not isinstance(box, dict):
                continue
            normalized_box = json.loads(json.dumps(box))
            original_varname = normalized_box.get("varname")
            source_varname = (
                original_varname.strip()
                if isinstance(original_varname, str)
                else ""
            )

            if source_varname:
                if source_varname in seen_source:
                    raise MaxMCPError(
                        ERROR_VALIDATION,
                        f"Source topology has duplicate varname '{source_varname}'.",
                        recoverable=False,
                    )
                seen_source.add(source_varname)
                if source_varname in used:
                    if not auto_rename_collisions:
                        raise MaxMCPError(
                            ERROR_PRECONDITION,
                            f"Varname collision detected for '{source_varname}'.",
                            hint="Retry with auto_rename_collisions=True.",
                            recoverable=False,
                        )
                    renamed = cls._generate_unique_varname(source_varname, used)
                    normalized_box["varname"] = renamed
                    remap[source_varname] = renamed
                    collisions += 1
                    used.add(renamed)
                else:
                    normalized_box["varname"] = source_varname
                    used.add(source_varname)
            else:
                generated = cls._generate_unique_varname(f"imp_obj_{idx+1}", used)
                normalized_box["varname"] = generated
                generated_varnames += 1
                used.add(generated)

            normalized_boxes.append({"box": normalized_box})

        valid_varnames = {
            row["box"]["varname"]
            for row in normalized_boxes
            if isinstance(row, dict)
            and isinstance(row.get("box"), dict)
            and isinstance(row["box"].get("varname"), str)
            and row["box"]["varname"]
        }

        normalized_lines: list[dict] = []
        skipped_lines = 0
        for row in topology.get("lines", []):
            if not isinstance(row, dict):
                skipped_lines += 1
                continue
            patchline = row.get("patchline", row)
            if not isinstance(patchline, dict):
                skipped_lines += 1
                continue
            source = list(patchline.get("source") or [])
            destination = list(patchline.get("destination") or [])
            if len(source) < 2 or len(destination) < 2:
                skipped_lines += 1
                continue

            src_var, src_idx = source[0], source[1]
            dst_var, dst_idx = destination[0], destination[1]

            if isinstance(src_var, str) and src_var in remap:
                src_var = remap[src_var]
            if isinstance(dst_var, str) and dst_var in remap:
                dst_var = remap[dst_var]

            if not isinstance(src_var, str) or not isinstance(dst_var, str):
                skipped_lines += 1
                continue

            if not isinstance(src_idx, int):
                try:
                    src_idx = int(src_idx)
                except Exception:
                    skipped_lines += 1
                    continue
            if not isinstance(dst_idx, int):
                try:
                    dst_idx = int(dst_idx)
                except Exception:
                    skipped_lines += 1
                    continue

            if src_var not in valid_varnames or dst_var not in valid_varnames:
                skipped_lines += 1
                continue

            normalized_lines.append(
                {"patchline": {"source": [src_var, src_idx], "destination": [dst_var, dst_idx]}}
            )

        return {
            "topology": {"boxes": normalized_boxes, "lines": normalized_lines},
            "varname_remap": remap,
            "collisions_count": collisions,
            "generated_varnames": generated_varnames,
            "skipped_lines": skipped_lines,
        }

    @staticmethod
    def _merge_topologies(base: dict, incoming: dict) -> dict:
        merged_boxes = json.loads(json.dumps(base.get("boxes", [])))
        merged_boxes.extend(json.loads(json.dumps(incoming.get("boxes", []))))

        line_seen: set[tuple[str, int, str, int]] = set()
        merged_lines: list[dict] = []
        for source_topology in (base, incoming):
            for row in source_topology.get("lines", []):
                if not isinstance(row, dict):
                    continue
                patchline = row.get("patchline", row)
                if not isinstance(patchline, dict):
                    continue
                source = patchline.get("source", [])
                destination = patchline.get("destination", [])
                if (
                    not isinstance(source, list)
                    or not isinstance(destination, list)
                    or len(source) < 2
                    or len(destination) < 2
                ):
                    continue
                if not isinstance(source[0], str) or not isinstance(destination[0], str):
                    continue
                try:
                    src_idx = int(source[1])
                    dst_idx = int(destination[1])
                except Exception:
                    continue
                key = (source[0], src_idx, destination[0], dst_idx)
                if key in line_seen:
                    continue
                line_seen.add(key)
                merged_lines.append(
                    {
                        "patchline": {
                            "source": [source[0], src_idx],
                            "destination": [destination[0], dst_idx],
                        }
                    }
                )
        return {"boxes": merged_boxes, "lines": merged_lines}


    def _topology_to_patch_payload(self, topology: dict) -> dict:
        template = self._patch_template_payload()
        if not isinstance(template, dict):
            template = {}
        patcher = template.get("patcher")
        if not isinstance(patcher, dict):
            patcher = {}
            template["patcher"] = patcher
        patcher["boxes"] = json.loads(json.dumps(topology.get("boxes", [])))
        patcher["lines"] = json.loads(json.dumps(topology.get("lines", [])))
        return template

    def _load_workspace_topology_sync(self, patch_path: Path) -> dict:
        if not patch_path.exists():
            return {"boxes": [], "lines": []}
        try:
            payload = json.loads(patch_path.read_text())
        except Exception as e:
            logging.warning(f"Failed to load workspace patch {patch_path}: {e}")
            return {"boxes": [], "lines": []}
        return self._extract_topology_from_payload(payload)

    def _write_workspace_topology_sync(self, target: str, topology: dict) -> dict:
        patch_path = self._workspace_path_for_target(target)
        if patch_path is None:
            return {
                "persisted": False,
                "reason": "host_target",
                "target": target,
            }
        self.session_dir.mkdir(parents=True, exist_ok=True)
        patch_payload = self._topology_to_patch_payload(topology)
        patch_path.write_text(json.dumps(patch_payload, indent=2))
        digest, object_count, connection_count = self._topology_hash(topology)
        return {
            "persisted": True,
            "target": target,
            "path": str(patch_path),
            "hash": digest,
            "object_count": object_count,
            "connection_count": connection_count,
        }

    async def persist_workspace_target(self, target: str | None = None, reason: str = "manual") -> dict:
        target_id = target or self.active_target
        if target_id not in {"active", "scratch"}:
            return {"persisted": False, "reason": "host_target", "target": target_id}
        if not self.maxmsp.sio.connected:
            return {"persisted": False, "reason": "bridge_disconnected", "target": target_id}
        try:
            topology = await self._capture_live_topology()
        except Exception as e:
            return {
                "persisted": False,
                "reason": "capture_failed",
                "target": target_id,
                "error": str(e),
            }
        try:
            result = await asyncio.to_thread(
                self._write_workspace_topology_sync,
                target_id,
                topology,
            )
            result["reason"] = reason
            return result
        except Exception as e:
            return {
                "persisted": False,
                "reason": "write_failed",
                "target": target_id,
                "error": str(e),
            }

    async def hydrate_workspace_target(self, target: str | None = None, reason: str = "manual") -> dict:
        target_id = target or self.active_target
        patch_path = self._workspace_path_for_target(target_id)
        if patch_path is None:
            return {"applied": False, "reason": "host_target", "target": target_id}

        topology = await asyncio.to_thread(self._load_workspace_topology_sync, patch_path)
        digest, object_count, connection_count = self._topology_hash(topology)
        if not self.maxmsp.sio.connected:
            return {
                "applied": False,
                "reason": "bridge_disconnected",
                "target": target_id,
                "path": str(patch_path),
                "hash": digest,
                "object_count": object_count,
                "connection_count": connection_count,
            }
        try:
            bridge_result = await self.maxmsp.send_request(
                {"action": "apply_topology_snapshot", "snapshot": topology},
                timeout=20.0,
            )
            return {
                "applied": True,
                "reason": reason,
                "target": target_id,
                "path": str(patch_path),
                "hash": digest,
                "object_count": object_count,
                "connection_count": connection_count,
                "bridge_result": bridge_result,
            }
        except Exception as e:
            return {
                "applied": False,
                "reason": "bridge_apply_failed",
                "target": target_id,
                "path": str(patch_path),
                "hash": digest,
                "object_count": object_count,
                "connection_count": connection_count,
                "error": str(e),
            }

    async def validate_patch_file(self, path: str, strict: bool = False) -> dict:
        try:
            resolved = self._resolve_patch_path(path)
            parsed = await asyncio.to_thread(self._load_patch_topology_sync, resolved)
        except MaxMCPError as e:
            return {
                "success": False,
                "detected_format": "invalid",
                "path": path,
                "error": e.to_dict(),
            }
        except Exception as e:
            return {
                "success": False,
                "detected_format": "invalid",
                "path": path,
                "error": {
                    "code": ERROR_INTERNAL,
                    "message": str(e),
                    "recoverable": True,
                    "details": {},
                },
            }

        topology = parsed["topology"]
        try:
            validation = self._normalize_import_topology(
                topology,
                reserved_varnames=set(),
                auto_rename_collisions=False,
            )
        except MaxMCPError as e:
            return {
                "success": False,
                "detected_format": parsed["format"],
                "path": str(resolved),
                "object_count": parsed["object_count"],
                "connection_count": parsed["connection_count"],
                "error": e.to_dict(),
            }
        warnings = []
        if validation.get("generated_varnames", 0) > 0:
            warnings.append(
                {
                    "code": ERROR_VALIDATION,
                    "message": (
                        f"{validation['generated_varnames']} objects were missing varnames and "
                        "would be generated during import."
                    ),
                }
            )
        if validation.get("skipped_lines", 0) > 0:
            warnings.append(
                {
                    "code": ERROR_VALIDATION,
                    "message": f"{validation['skipped_lines']} lines were invalid and ignored.",
                }
            )

        if strict and warnings:
            return {
                "success": False,
                "detected_format": parsed["format"],
                "path": str(resolved),
                "object_count": parsed["object_count"],
                "connection_count": parsed["connection_count"],
                "warnings": warnings,
                "error": {
                    "code": ERROR_VALIDATION,
                    "message": "Strict validation failed due to warnings.",
                    "recoverable": True,
                    "details": {"warnings": warnings},
                },
            }

        return {
            "success": True,
            "detected_format": parsed["format"],
            "path": str(resolved),
            "object_count": parsed["object_count"],
            "connection_count": parsed["connection_count"],
            "hash": parsed["hash"],
            "warnings": warnings,
        }

    async def load_patch_from_path(
        self,
        path: str,
        *,
        target: str = "active",
        mode: str = "replace",
        auto_rename_collisions: bool = True,
        create_checkpoint_before_load: bool = True,
        checkpoint_label: str = "pre_load_import",
        idempotency_key: str = "",
    ) -> dict:
        target_id = target.strip().lower()
        if target_id not in {"active", "scratch"}:
            return {
                "success": False,
                "error": {
                    "code": ERROR_PRECONDITION,
                    "message": "target must be 'active' or 'scratch'. host imports are blocked.",
                    "recoverable": False,
                    "details": {"target": target},
                },
            }

        mode_normalized = mode.strip().lower()
        if mode_normalized not in {"replace", "merge", "fail_if_not_empty"}:
            return {
                "success": False,
                "error": {
                    "code": ERROR_VALIDATION,
                    "message": "mode must be one of: replace, merge, fail_if_not_empty.",
                    "recoverable": True,
                    "details": {"mode": mode},
                },
            }

        try:
            readiness = await self.ensure_runtime_ready()
        except Exception as e:
            return self._operation_error(
                operation="load_patch_from_path",
                action="ensure_runtime_ready",
                error=e,
                details={"target": target_id, "mode": mode_normalized},
            )
        if not readiness.get("ready"):
            return {
                "success": False,
                "error": {
                    "code": ERROR_BRIDGE_UNAVAILABLE,
                    "message": readiness.get("error", "Bridge runtime is not ready."),
                    "recoverable": True,
                    "details": readiness,
                },
            }

        capability_error = self._check_required_capabilities(
            required_actions={"set_workspace_target", "get_objects_in_patch", "apply_topology_snapshot"},
            operation="load_patch_from_path",
        )
        if capability_error:
            return capability_error

        switch_result = None
        if self.active_target != target_id:
            try:
                switch_result = await self.set_active_target(target_id)
            except Exception as e:
                return self._operation_error(
                    operation="load_patch_from_path",
                    action="set_workspace_target",
                    error=e,
                    details={"target": target_id, "mode": mode_normalized},
                )
            if not switch_result.get("success"):
                return {
                    "success": False,
                    "error": {
                        "code": ERROR_PRECONDITION,
                        "message": "Failed to switch target workspace before import.",
                        "recoverable": False,
                        "details": switch_result,
                    },
                }

        try:
            resolved_path = self._resolve_patch_path(path)
            source_payload = await asyncio.to_thread(self._load_patch_topology_sync, resolved_path)
        except MaxMCPError as e:
            return {"success": False, "error": e.to_dict()}
        except Exception as e:
            return {
                "success": False,
                "error": {
                    "code": ERROR_INTERNAL,
                    "message": str(e),
                    "recoverable": True,
                    "details": {"path": path},
                },
            }

        source_topology = source_payload["topology"]
        try:
            existing_topology = await self._capture_live_topology()
        except Exception as e:
            return self._operation_error(
                operation="load_patch_from_path",
                action="get_objects_in_patch",
                error=e,
                details={"target": target_id, "mode": mode_normalized},
            )
        if mode_normalized == "fail_if_not_empty" and not self._is_topology_empty(existing_topology):
            return {
                "success": False,
                "error": {
                    "code": ERROR_PRECONDITION,
                    "message": "Target workspace is not empty.",
                    "hint": "Use mode='replace' or clear workspace first.",
                    "recoverable": False,
                    "details": {
                        "target": target_id,
                        "object_count": len(existing_topology.get("boxes", [])),
                        "connection_count": len(existing_topology.get("lines", [])),
                    },
                },
            }

        reserved = self._topology_varnames(existing_topology) if mode_normalized == "merge" else set()
        try:
            normalized_source = self._normalize_import_topology(
                source_topology,
                reserved_varnames=reserved,
                auto_rename_collisions=auto_rename_collisions,
            )
        except MaxMCPError as e:
            return {"success": False, "error": e.to_dict()}
        except Exception as e:
            return {
                "success": False,
                "error": {
                    "code": ERROR_INTERNAL,
                    "message": str(e),
                    "recoverable": True,
                    "details": {},
                },
            }

        incoming_topology = normalized_source["topology"]
        if mode_normalized == "merge":
            final_topology = self._merge_topologies(existing_topology, incoming_topology)
        else:
            final_topology = incoming_topology

        checkpoint_result = None
        if create_checkpoint_before_load:
            checkpoint_result = await self.create_checkpoint(label=checkpoint_label or "pre_load_import")
            if not checkpoint_result.get("success"):
                return {
                    "success": False,
                    "error": {
                        "code": ERROR_PRECONDITION,
                        "message": "Failed to create pre-load checkpoint.",
                        "recoverable": False,
                        "details": checkpoint_result,
                    },
                }

        try:
            apply_result = await self.maxmsp.send_request(
                {"action": "apply_topology_snapshot", "snapshot": final_topology},
                timeout=25.0,
                idempotency_key=idempotency_key or None,
            )
        except Exception as e:
            return self._operation_error(
                operation="load_patch_from_path",
                action="apply_topology_snapshot",
                error=e,
                details={"target": target_id, "mode": mode_normalized},
            )

        persist_result = await self.persist_workspace_target(
            target=target_id,
            reason=f"load_patch_from_path:{mode_normalized}",
        )
        twin_sync = await self.sync_patch_twin(reason=f"load_patch_from_path:{mode_normalized}")
        post_load_drift = await self.check_patch_drift(auto_resync=False)
        final_hash, final_objects, final_connections = self._topology_hash(final_topology)

        return {
            "success": True,
            "source_path": str(resolved_path),
            "target": target_id,
            "mode": mode_normalized,
            "switch_result": switch_result,
            "checkpoint": checkpoint_result,
            "apply_result": apply_result,
            "persist_result": persist_result,
            "twin_sync": twin_sync,
            "post_load_drift": post_load_drift,
            "import_summary": {
                "detected_format": source_payload["format"],
                "source_hash": source_payload["hash"],
                "final_hash": final_hash,
                "objects_in_source": source_payload["object_count"],
                "lines_in_source": source_payload["connection_count"],
                "objects_loaded": (
                    apply_result.get("restored_boxes")
                    if isinstance(apply_result, dict)
                    else final_objects
                ),
                "lines_loaded": (
                    apply_result.get("restored_lines")
                    if isinstance(apply_result, dict)
                    else final_connections
                ),
                "skipped_objects": (
                    apply_result.get("skipped_boxes", 0)
                    if isinstance(apply_result, dict)
                    else 0
                ),
                "skipped_lines": (
                    normalized_source.get("skipped_lines", 0)
                    + (apply_result.get("skipped_lines", 0) if isinstance(apply_result, dict) else 0)
                ),
                "collisions_count": normalized_source.get("collisions_count", 0),
                "varname_remap": normalized_source.get("varname_remap", {}),
            },
        }

    async def save_patch_to_path(
        self,
        path: str,
        *,
        target: str = "",
        overwrite: bool = False,
    ) -> dict:
        target_id = (target or self.active_target).strip().lower()
        if target_id not in {"active", "scratch"}:
            return {
                "success": False,
                "error": {
                    "code": ERROR_PRECONDITION,
                    "message": "target must be 'active' or 'scratch'.",
                    "recoverable": False,
                    "details": {"target": target},
                },
            }

        destination_raw = (path or "").strip()
        if not destination_raw:
            return {
                "success": False,
                "error": {
                    "code": ERROR_VALIDATION,
                    "message": "path must be a non-empty string.",
                    "recoverable": True,
                    "details": {},
                },
            }

        destination = Path(destination_raw).expanduser()
        if not destination.is_absolute():
            destination = (Path.cwd() / destination).resolve()
        else:
            destination = destination.resolve()

        if destination.exists() and destination.is_dir():
            return {
                "success": False,
                "error": {
                    "code": ERROR_VALIDATION,
                    "message": f"Destination is a directory: {destination}",
                    "recoverable": True,
                    "details": {},
                },
            }
        if destination.exists() and not overwrite:
            return {
                "success": False,
                "error": {
                    "code": ERROR_PRECONDITION,
                    "message": f"Destination already exists: {destination}",
                    "hint": "Set overwrite=True to replace existing file.",
                    "recoverable": True,
                    "details": {},
                },
            }
        if destination.suffix.lower() not in {".maxpat", ".json"}:
            return {
                "success": False,
                "error": {
                    "code": ERROR_VALIDATION,
                    "message": (
                        f"Unsupported destination extension '{destination.suffix}'. "
                        "Expected .maxpat or .json."
                    ),
                    "recoverable": True,
                    "details": {},
                },
            }
        try:
            self._validate_patch_path_policy(destination, purpose="patch_write")
        except MaxMCPError as e:
            return {"success": False, "error": e.to_dict()}

        try:
            readiness = await self.ensure_runtime_ready()
        except Exception as e:
            return self._operation_error(
                operation="save_patch_to_path",
                action="ensure_runtime_ready",
                error=e,
                details={"target": target_id, "path": str(destination)},
            )
        if not readiness.get("ready"):
            return {
                "success": False,
                "error": {
                    "code": ERROR_BRIDGE_UNAVAILABLE,
                    "message": readiness.get("error", "Bridge runtime is not ready."),
                    "recoverable": True,
                    "details": readiness,
                },
            }

        capability_error = self._check_required_capabilities(
            required_actions={"set_workspace_target", "get_objects_in_patch"},
            operation="save_patch_to_path",
        )
        if capability_error:
            return capability_error

        switch_result = None
        if self.active_target != target_id:
            try:
                switch_result = await self.set_active_target(target_id)
            except Exception as e:
                return self._operation_error(
                    operation="save_patch_to_path",
                    action="set_workspace_target",
                    error=e,
                    details={"target": target_id, "path": str(destination)},
                )
            if not switch_result.get("success"):
                return {
                    "success": False,
                    "error": {
                        "code": ERROR_PRECONDITION,
                        "message": "Failed to switch target workspace before export.",
                        "recoverable": False,
                        "details": switch_result,
                    },
                }

        try:
            topology = await self._capture_live_topology()
        except Exception as e:
            return self._operation_error(
                operation="save_patch_to_path",
                action="get_objects_in_patch",
                error=e,
                details={"target": target_id, "path": str(destination)},
            )
        digest, object_count, connection_count = self._topology_hash(topology)
        patch_payload = self._topology_to_patch_payload(topology)

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(patch_payload, indent=2))
        except Exception as e:
            return self._operation_error(
                operation="save_patch_to_path",
                action="write_file",
                error=e,
                details={"target": target_id, "path": str(destination)},
            )
        return {
            "success": True,
            "target": target_id,
            "path": str(destination),
            "overwrite": overwrite,
            "hash": digest,
            "object_count": object_count,
            "connection_count": connection_count,
            "switch_result": switch_result,
        }

    def _workspace_varname_for_target(self, target: str) -> str | None:
        if target == "active":
            return self.workspace_active_varname
        if target == "scratch":
            return self.workspace_scratch_varname
        return None

    def _workspace_display_name_for_target(self, target: str) -> str:
        if target == "scratch":
            return f"mcp_scratch_{self.session_id}"
        return f"mcp_active_{self.session_id}"

    async def _apply_target_to_bridge(self) -> dict:
        if not self.maxmsp.sio.connected:
            return {"applied": False, "reason": "bridge_disconnected"}

        payload: dict[str, Any] = {
            "action": "set_workspace_target",
            "target_id": self.active_target,
        }
        workspace_varname = self._workspace_varname_for_target(self.active_target)
        if workspace_varname:
            payload["workspace_varname"] = workspace_varname
            payload["workspace_name"] = self._workspace_display_name_for_target(
                self.active_target
            )
        response = await self.maxmsp.send_request(payload, timeout=3.0)
        if isinstance(response, dict):
            response.setdefault("applied", True)
            return response
        return {"applied": True, "response": response}

    async def set_active_target(self, target: str) -> dict:
        allowed = {"host", "active", "scratch"}
        if target not in allowed:
            return {
                "success": False,
                "error": f"Invalid target '{target}'. Use one of {sorted(allowed)}.",
            }

        previous_target = self.active_target
        persist_result = {"persisted": False, "reason": "not_switched", "target": previous_target}
        if previous_target != target and previous_target in {"active", "scratch"}:
            persist_result = await self.persist_workspace_target(
                target=previous_target,
                reason=f"switch:{previous_target}->{target}",
            )

        self.active_target = target
        apply_result = await self._apply_target_to_bridge()
        hydrate_result = {"applied": False, "reason": "not_switched", "target": target}
        if previous_target != target and target in {"active", "scratch"}:
            hydrate_result = await self.hydrate_workspace_target(
                target=target,
                reason=f"switch:{previous_target}->{target}",
            )

        twin_sync = None
        if self.maxmsp.sio.connected and target in {"active", "scratch"}:
            twin_sync = await self.sync_patch_twin(reason=f"switch_target:{target}")

        return {
            "success": True,
            "active_target": self.active_target,
            "previous_target": previous_target,
            "apply_result": apply_result,
            "persist_result": persist_result,
            "hydrate_result": hydrate_result,
            "twin_sync": twin_sync,
            "targets": self.list_patch_targets(),
        }

    def _write_state(self, payload: dict) -> None:
        self._ensure_state_dir()
        serializable = dict(payload)
        serializable["updated_at"] = int(time.time())
        try:
            self.state_file.write_text(json.dumps(serializable, indent=2))
        except Exception as e:
            logging.warning(f"Failed to write runtime state file: {e}")

    def _ensure_node_dependencies_sync(self) -> dict:
        if not self.npm_auto_install:
            return {"attempted": False, "ready": self.npm_sentinel.exists()}

        if self.npm_sentinel.exists():
            return {"attempted": False, "ready": True}

        if not self.npm_project_dir.exists():
            return {
                "attempted": False,
                "ready": False,
                "error": f"Node project directory not found: {self.npm_project_dir}",
            }

        npm_bin = os.environ.get("MAXMCP_NPM_BIN", "npm")
        try:
            logging.info(f"Installing Node dependencies in {self.npm_project_dir}")
            subprocess.run(
                [npm_bin, "install", "--no-audit", "--no-fund"],
                cwd=self.npm_project_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return {"attempted": True, "ready": self.npm_sentinel.exists()}
        except Exception as e:
            return {"attempted": True, "ready": False, "error": str(e)}

    def _launch_max_sync(self, patch_to_open: Path) -> dict:
        if not self.max_app_path.exists():
            return {
                "launched": False,
                "error": (
                    f"Max app not found at {self.max_app_path}. "
                    "Set MAXMCP_MAX_APP to your Max.app path."
                ),
            }

        now = time.monotonic()
        if now - self._last_launch_at < self._launch_cooldown_seconds:
            return {"launched": False, "throttled": True}

        self._last_launch_at = now
        try:
            subprocess.run(
                ["open", "-a", str(self.max_app_path), str(patch_to_open)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"launched": True}
        except Exception as e:
            return {"launched": False, "error": str(e)}

    async def _wait_for_bridge(self, timeout_seconds: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            connected = await self.maxmsp.start_server()
            if connected:
                return True
            await asyncio.sleep(0.5)
        return False

    async def collect_status(self, check_bridge: bool = True) -> dict:
        host_patch = self._resolve_host_patch()
        status = {
            "managed_mode": self.managed_mode,
            "endpoint": self.maxmsp.endpoint,
            "bridge_connected": self.maxmsp.sio.connected,
            "max_app_path": str(self.max_app_path),
            "max_app_exists": self.max_app_path.exists(),
            "host_patch_path": str(host_patch) if host_patch else None,
            "host_patch_exists": bool(host_patch and host_patch.exists()),
            "node_modules_ready": self.npm_sentinel.exists(),
            "state_file": str(self.state_file),
            "session_id": self.session_id,
            "session_dir": str(self.session_dir),
            "session_active_patch": str(self.session_active_patch),
            "session_active_patch_exists": self.session_active_patch.exists(),
            "session_scratch_patch": str(self.session_scratch_patch),
            "session_scratch_patch_exists": self.session_scratch_patch.exists(),
            "active_target": self.active_target,
            "active_workspace_varname": self._workspace_varname_for_target(self.active_target),
            "checkpoint_count": len(self.checkpoints),
            "checkpoint_journal_file": str(self.checkpoints_file),
            "checkpoint_journal_exists": self.checkpoints_file.exists(),
            "enforce_patch_roots": self.enforce_patch_roots,
            "allowed_patch_roots": [str(root) for root in self.allowed_patch_roots],
            "twin": self._twin_status_payload(),
            "targets": self.list_patch_targets(),
        }
        if self.hygiene_manager is not None:
            status["hygiene_policy"] = self.hygiene_manager.policy_snapshot()
            status["hygiene_last_run_at"] = self.hygiene_manager.last_run_at
            status["hygiene_last_summary"] = self.hygiene_manager.last_summary
        if check_bridge and self.maxmsp.sio.connected:
            try:
                ping = await self.maxmsp.send_request({"action": "health_ping"}, timeout=2.0)
                status["bridge_healthy"] = bool(
                    ping == "pong" or (isinstance(ping, dict) and ping.get("ok"))
                )
                status["bridge_ping"] = ping
            except Exception as e:
                status["bridge_healthy"] = False
                status["bridge_ping_error"] = str(e)
            try:
                workspace_status = await self.maxmsp.send_request(
                    {"action": "workspace_status"},
                    timeout=2.0,
                )
                status["workspace_status"] = workspace_status
            except Exception as e:
                status["workspace_status_error"] = str(e)
        return status

    async def ensure_runtime_ready(self) -> dict:
        if not self.managed_mode:
            return await self.collect_status(check_bridge=False)

        async with self._lock:
            self._ensure_state_dir()
            await asyncio.to_thread(self._ensure_session_patches_sync)
            checkpoint_journal = await asyncio.to_thread(self._load_checkpoint_journal_sync)
            host_patch = self._resolve_host_patch()
            if not host_patch:
                status = await self.collect_status(check_bridge=False)
                status["ready"] = False
                status["error"] = (
                    "No host patch found. Expected one of: "
                    f"{self.host_patch_path} or {self.fallback_patch_path}."
                )
                self._write_state(status)
                return status

            deps = await asyncio.to_thread(self._ensure_node_dependencies_sync)
            if not deps.get("ready"):
                status = await self.collect_status(check_bridge=False)
                status["ready"] = False
                status["error"] = (
                    "Node dependencies for Max bridge are not ready. "
                    f"{deps.get('error', 'Unknown npm failure')}"
                )
                self._write_state(status)
                return status

            if not await self.maxmsp.start_server():
                launch_info = await asyncio.to_thread(self._launch_max_sync, host_patch)
                if launch_info.get("error"):
                    status = await self.collect_status(check_bridge=False)
                    status["ready"] = False
                    status["error"] = launch_info["error"]
                    self._write_state(status)
                    return status

                await self._wait_for_bridge(timeout_seconds=20.0)

            workspace_apply_error = None
            workspace_hydrate_error = None
            workspace_hydrate_result = None
            if self.maxmsp.sio.connected:
                try:
                    await self._apply_target_to_bridge()
                except Exception as e:
                    workspace_apply_error = str(e)
                if self.active_target in {"active", "scratch"}:
                    try:
                        workspace_hydrate_result = await self.hydrate_workspace_target(
                            target=self.active_target,
                            reason="runtime_ready",
                        )
                    except Exception as e:
                        workspace_hydrate_error = str(e)

            status = await self.collect_status(check_bridge=True)
            status["ready"] = bool(status.get("bridge_connected"))
            status["checkpoint_journal"] = checkpoint_journal
            if not status["ready"]:
                status["error"] = self.maxmsp._offline_error_message()
            if workspace_apply_error:
                status["workspace_apply_error"] = workspace_apply_error
            if workspace_hydrate_result is not None:
                status["workspace_hydrate"] = workspace_hydrate_result
            if workspace_hydrate_error:
                status["workspace_hydrate_error"] = workspace_hydrate_error
            if status["ready"]:
                twin_sync = await self.sync_patch_twin(reason="runtime_ready")
                status["twin_sync"] = twin_sync
                if self.hygiene_manager is not None:
                    startup_gc = await self.hygiene_manager.run_startup_cleanup_once()
                    if startup_gc:
                        status["hygiene_startup_cleanup"] = startup_gc
            self._write_state(status)
            return status

    async def recover_bridge(self) -> dict:
        if self.maxmsp.sio.connected:
            await self.maxmsp.disconnect()
        status = await self.ensure_runtime_ready()
        status["recovered"] = bool(status.get("ready"))
        return status

    def list_patch_targets(self) -> list:
        host_patch = self._resolve_host_patch()
        host = {
            "id": "host",
            "description": "Managed MCP bridge host patch",
            "path": str(host_patch) if host_patch else None,
            "exists": bool(host_patch and host_patch.exists()),
            "selected": self.active_target == "host",
        }
        active = {
            "id": "active",
            "description": "Per-session editable workspace target",
            "path": str(self.session_active_patch),
            "exists": self.session_active_patch.exists(),
            "workspace_varname": self.workspace_active_varname,
            "selected": self.active_target == "active",
        }
        scratch = {
            "id": "scratch",
            "description": "Per-session fallback workspace target",
            "path": str(self.session_scratch_patch),
            "exists": self.session_scratch_patch.exists(),
            "workspace_varname": self.workspace_scratch_varname,
            "selected": self.active_target == "scratch",
        }
        return [host, active, scratch]


class MaxHygieneManager:
    """System-wide Max process/session visibility and cleanup manager."""

    def __init__(self, runtime: MaxRuntimeManager, maxmsp: MaxMSPConnection):
        self.runtime = runtime
        self.maxmsp = maxmsp
        self.state_dir = MAXMCP_STATE_DIR
        self.report_file = self.state_dir / "hygiene_report.json"
        self.auto_cleanup = MAXMCP_HYGIENE_AUTO_CLEANUP
        self.scope = (
            MAXMCP_HYGIENE_SCOPE
            if MAXMCP_HYGIENE_SCOPE in {"all_max_instances", "managed_only"}
            else "all_max_instances"
        )
        self.mode = (
            MAXMCP_HYGIENE_MODE
            if MAXMCP_HYGIENE_MODE in {"aggressive", "preview"}
            else "aggressive"
        )
        self.stale_seconds = max(60, MAXMCP_HYGIENE_STALE_SECONDS)
        self.startup_sweep = MAXMCP_HYGIENE_STARTUP_SWEEP
        self.report_max = max(10, MAXMCP_HYGIENE_REPORT_MAX)
        self.max_kills_per_sweep = max(1, MAXMCP_HYGIENE_MAX_KILLS_PER_SWEEP)
        self.enable_window_scan = MAXMCP_HYGIENE_ENABLE_WINDOW_SCAN
        self.loop_interval_seconds = max(10.0, MAXMCP_HYGIENE_LOOP_INTERVAL_SECONDS)
        self.keep_recent_sessions = max(0, MAXMCP_HYGIENE_KEEP_RECENT_SESSIONS)
        self.last_run_at: float | None = None
        self.last_summary: dict = {}
        self._events = deque(maxlen=self.report_max)
        self._lock = asyncio.Lock()
        self._startup_cleanup_ran = False
        self._load_report_sync()

    def policy_snapshot(self) -> dict:
        return {
            "auto_cleanup": self.auto_cleanup,
            "scope": self.scope,
            "mode": self.mode,
            "stale_seconds": self.stale_seconds,
            "startup_sweep": self.startup_sweep,
            "report_max": self.report_max,
            "max_kills_per_sweep": self.max_kills_per_sweep,
            "enable_window_scan": self.enable_window_scan,
            "loop_interval_seconds": self.loop_interval_seconds,
            "keep_recent_sessions": self.keep_recent_sessions,
            "report_file": str(self.report_file),
        }

    def set_policy(
        self,
        *,
        auto_cleanup: bool,
        scope: str,
        mode: str,
        stale_seconds: int,
        startup_sweep: bool,
    ) -> dict:
        scope_value = scope.strip().lower()
        if scope_value not in {"all_max_instances", "managed_only"}:
            raise MaxMCPError(
                ERROR_VALIDATION,
                "scope must be one of: all_max_instances, managed_only.",
                recoverable=True,
            )
        mode_value = mode.strip().lower()
        if mode_value not in {"aggressive", "preview"}:
            raise MaxMCPError(
                ERROR_VALIDATION,
                "mode must be one of: aggressive, preview.",
                recoverable=True,
            )
        self.auto_cleanup = bool(auto_cleanup)
        self.scope = scope_value
        self.mode = mode_value
        self.stale_seconds = max(60, int(stale_seconds))
        self.startup_sweep = bool(startup_sweep)
        return self.policy_snapshot()

    def _ensure_state_dir_sync(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _load_report_sync(self) -> None:
        self._ensure_state_dir_sync()
        if not self.report_file.exists():
            return
        try:
            payload = json.loads(self.report_file.read_text())
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        rows = payload.get("events", [])
        if isinstance(rows, list):
            for row in rows[-self.report_max :]:
                if isinstance(row, dict):
                    self._events.append(row)
        summary = payload.get("last_summary")
        if isinstance(summary, dict):
            self.last_summary = summary
        last_run = payload.get("last_run_at")
        if isinstance(last_run, (int, float)):
            self.last_run_at = float(last_run)

    def _persist_report_sync(self) -> None:
        self._ensure_state_dir_sync()
        payload = {
            "updated_at": time.time(),
            "last_run_at": self.last_run_at,
            "last_summary": self.last_summary,
            "events": list(self._events)[-self.report_max :],
            "policy": self.policy_snapshot(),
        }
        self.report_file.write_text(json.dumps(payload, indent=2))

    @staticmethod
    def _path_within(path: Path, root: Path) -> bool:
        try:
            return path == root or root in path.parents
        except Exception:
            return False

    @staticmethod
    def _is_max_command(command: str) -> bool:
        cmd = (command or "").strip()
        if not cmd:
            return False
        lower = cmd.lower()
        if "/max.app/" in lower or "/contents/macos/max" in lower:
            return True
        token = lower.split()[0]
        return token.endswith("/max") or token == "max"

    def _read_process_table_sync(self) -> list[dict]:
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,etimes=,%cpu=,rss=,command="],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3.0,
            )
        except Exception:
            return []
        rows: list[dict] = []
        for raw in out.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
                elapsed_seconds = int(parts[2])
                cpu_pct = float(parts[3])
                rss_kb = int(parts[4])
            except Exception:
                continue
            rows.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "elapsed_seconds": elapsed_seconds,
                    "cpu_pct": cpu_pct,
                    "rss_mb": round(rss_kb / 1024.0, 3),
                    "command": parts[5],
                }
            )
        return rows

    def _discover_bridge_listener_pid_sync(self) -> int | None:
        port = str(getattr(self.maxmsp, "server_port", "") or "").strip()
        if not port:
            return None
        try:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            )
        except Exception:
            return None
        for line in out.stdout.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                return int(candidate)
            except Exception:
                continue
        return None

    def _discover_bridge_owner_pid_sync(
        self,
        process_index: dict[int, dict],
        max_pids: set[int],
    ) -> int | None:
        listener_pid = self._discover_bridge_listener_pid_sync()
        if listener_pid is None:
            return None
        pid = listener_pid
        visited = set()
        while pid and pid not in visited:
            visited.add(pid)
            if pid in max_pids:
                return pid
            row = process_index.get(pid)
            if not row:
                break
            pid = row.get("ppid")
            if not isinstance(pid, int) or pid <= 0:
                break
        return None

    def _scan_open_documents_sync(self) -> dict:
        if not self.enable_window_scan:
            return {"available": False, "reason": "disabled", "documents": []}
        if os.name != "posix" or "darwin" not in os.uname().sysname.lower():
            return {"available": False, "reason": "unsupported_platform", "documents": []}
        script = (
            "const app = Application('Max');"
            "app.includeStandardAdditions = true;"
            "let docs = [];"
            "try {"
            "  docs = app.documents().map(function(d) {"
            "    let name = '';"
            "    let path = '';"
            "    try { name = d.name(); } catch (e) {}"
            "    try { const f = d.file(); if (f) { path = f.toString(); } } catch (e) {}"
            "    return {name: String(name || ''), path: String(path || '')};"
            "  });"
            "} catch (e) { docs = []; }"
            "JSON.stringify({documents: docs});"
        )
        try:
            out = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", script],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3.0,
            )
        except Exception as e:
            return {"available": False, "reason": f"osascript_error:{e}", "documents": []}
        if out.returncode != 0:
            reason = (out.stderr or "").strip() or f"osascript_exit_{out.returncode}"
            return {"available": False, "reason": reason, "documents": []}
        try:
            payload = json.loads(out.stdout.strip() or "{}")
        except Exception as e:
            return {"available": False, "reason": f"parse_error:{e}", "documents": []}
        docs = payload.get("documents") if isinstance(payload, dict) else []
        if not isinstance(docs, list):
            docs = []

        host_patch = self.runtime._resolve_host_patch()
        rows = []
        for item in docs:
            if not isinstance(item, dict):
                continue
            raw_path = str(item.get("path") or "").strip()
            name = str(item.get("name") or "").strip()
            is_managed_patch = False
            session_id_guess = None
            if raw_path and raw_path.startswith("/"):
                p = Path(raw_path).expanduser()
                try:
                    resolved = p.resolve()
                except Exception:
                    resolved = p
                if host_patch is not None and resolved == host_patch:
                    is_managed_patch = True
                if self._path_within(resolved, self.runtime.sessions_root):
                    is_managed_patch = True
                    try:
                        rel = resolved.relative_to(self.runtime.sessions_root)
                        if len(rel.parts) > 0:
                            session_id_guess = rel.parts[0]
                    except Exception:
                        session_id_guess = None
            rows.append(
                {
                    "name": name,
                    "path": raw_path or None,
                    "is_managed_patch": is_managed_patch,
                    "session_id_guess": session_id_guess,
                }
            )
        return {"available": True, "reason": None, "documents": rows}

    @staticmethod
    def _session_dir_size_bytes(path: Path) -> int:
        total = 0
        for root, _dirs, files in os.walk(path):
            for filename in files:
                try:
                    total += (Path(root) / filename).stat().st_size
                except Exception:
                    continue
        return total

    def _discover_managed_session_dirs_sync(self) -> list[dict]:
        sessions: list[dict] = []
        root = self.runtime.sessions_root
        now = time.time()
        if not root.exists():
            return sessions
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            active = entry / "active.maxpat"
            scratch = entry / "scratch.maxpat"
            checkpoints = entry / "checkpoints.json"
            mtimes = [entry.stat().st_mtime]
            for candidate in (active, scratch, checkpoints):
                if candidate.exists():
                    mtimes.append(candidate.stat().st_mtime)
            mtime_epoch = max(mtimes)
            age_seconds = max(0.0, now - mtime_epoch)
            session_id = entry.name
            is_current = session_id == self.runtime.session_id
            sessions.append(
                {
                    "session_id": session_id,
                    "path": str(entry),
                    "mtime_epoch": mtime_epoch,
                    "age_seconds": round(age_seconds, 3),
                    "has_active": active.exists(),
                    "has_scratch": scratch.exists(),
                    "has_checkpoints": checkpoints.exists(),
                    "is_current_runtime_session": is_current,
                    "is_stale": (not is_current) and age_seconds >= self.stale_seconds,
                    "size_bytes": self._session_dir_size_bytes(entry),
                }
            )
        sessions.sort(key=lambda row: row.get("mtime_epoch", 0.0), reverse=True)
        return sessions

    def _classify_process(self, row: dict, bridge_owner_pid: int | None) -> str:
        pid = row.get("pid")
        command = str(row.get("command") or "").lower()
        host_patch = self.runtime._resolve_host_patch()
        if isinstance(pid, int) and bridge_owner_pid is not None and pid == bridge_owner_pid:
            return "managed_bridge_owner"
        if host_patch is not None and str(host_patch).lower() in command:
            return "managed_host_patch"
        sessions_root = str(self.runtime.sessions_root).lower()
        if sessions_root and sessions_root in command:
            return "managed_workspace_patch"
        return "non_managed_max"

    def _is_process_stale(self, row: dict, classification: str) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        elapsed_seconds = float(row.get("elapsed_seconds", 0.0))
        cpu_pct = float(row.get("cpu_pct", 0.0))
        if elapsed_seconds >= self.stale_seconds:
            reasons.append("elapsed_over_threshold")
        if cpu_pct <= 0.5:
            reasons.append("low_cpu")
        stale = "elapsed_over_threshold" in reasons and "low_cpu" in reasons
        if classification == "managed_bridge_owner" and self.maxmsp.sio.connected:
            stale = False
            reasons.append("active_bridge_connection")
        return stale, reasons

    def _build_inventory_sync(
        self,
        *,
        include_windows: bool,
        include_runtime_state: bool,
    ) -> dict:
        now = time.time()
        all_processes = self._read_process_table_sync()
        process_index = {
            row["pid"]: row
            for row in all_processes
            if isinstance(row, dict) and isinstance(row.get("pid"), int)
        }
        max_process_rows = [row for row in all_processes if self._is_max_command(row.get("command", ""))]
        max_pid_set = {row["pid"] for row in max_process_rows if isinstance(row.get("pid"), int)}
        bridge_owner_pid = self._discover_bridge_owner_pid_sync(process_index, max_pid_set)

        process_rows: list[dict] = []
        for row in max_process_rows:
            classification = self._classify_process(row, bridge_owner_pid)
            stale, stale_reasons = self._is_process_stale(row, classification)
            process_rows.append(
                {
                    "pid": row.get("pid"),
                    "ppid": row.get("ppid"),
                    "elapsed_seconds": row.get("elapsed_seconds"),
                    "cpu_pct": row.get("cpu_pct"),
                    "rss_mb": row.get("rss_mb"),
                    "command": row.get("command"),
                    "classified_as": classification,
                    "idle_seconds_estimate": row.get("elapsed_seconds"),
                    "is_stale": stale,
                    "stale_reasons": stale_reasons,
                    "is_bridge_owner": bool(
                        bridge_owner_pid is not None and row.get("pid") == bridge_owner_pid
                    ),
                }
            )

        windows = {"available": False, "reason": "not_requested", "documents": []}
        if include_windows:
            windows = self._scan_open_documents_sync()

        sessions = self._discover_managed_session_dirs_sync()
        runtime_state = {}
        if include_runtime_state:
            runtime_state = {
                "session_id": self.runtime.session_id,
                "active_target": self.runtime.active_target,
                "bridge_connected": bool(self.maxmsp.sio.connected),
                "state_file": str(self.runtime.state_file),
                "last_bridge_response_at": self.maxmsp.last_response_at,
            }
        stale_process_count = sum(1 for row in process_rows if row.get("is_stale"))
        stale_session_count = sum(1 for row in sessions if row.get("is_stale"))
        return {
            "now_epoch": now,
            "policy": self.policy_snapshot(),
            "max_processes": process_rows,
            "open_patch_windows": windows,
            "managed_sessions_on_disk": sessions,
            "current_runtime": runtime_state,
            "summary": {
                "max_process_count": len(process_rows),
                "stale_process_count": stale_process_count,
                "managed_session_count": len(sessions),
                "stale_session_count": stale_session_count,
                "bridge_owner_pid": bridge_owner_pid,
            },
        }

    async def list_system_sessions(
        self,
        *,
        include_windows: bool = True,
        include_runtime_state: bool = True,
    ) -> dict:
        return await asyncio.to_thread(
            self._build_inventory_sync,
            include_windows=include_windows,
            include_runtime_state=include_runtime_state,
        )

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

    @classmethod
    def _wait_for_pid_exit(cls, pid: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not cls._pid_exists(pid):
                return True
            time.sleep(0.1)
        return not cls._pid_exists(pid)

    @classmethod
    def _kill_pid_sync(cls, pid: int, force: bool = True) -> dict:
        if not cls._pid_exists(pid):
            return {"pid": pid, "terminated": True, "already_gone": True}
        try:
            os.kill(pid, signal.SIGTERM)
            exited = cls._wait_for_pid_exit(pid, timeout_seconds=3.0)
            if exited:
                return {"pid": pid, "terminated": True, "signal": "SIGTERM"}
            if force:
                os.kill(pid, signal.SIGKILL)
                killed = cls._wait_for_pid_exit(pid, timeout_seconds=2.0)
                return {
                    "pid": pid,
                    "terminated": bool(killed),
                    "signal": "SIGKILL",
                    "escalated": True,
                }
            return {"pid": pid, "terminated": False, "signal": "SIGTERM", "timeout": True}
        except PermissionError:
            return {"pid": pid, "terminated": False, "error": "permission_denied"}
        except ProcessLookupError:
            return {"pid": pid, "terminated": True, "already_gone": True}
        except Exception as e:
            return {"pid": pid, "terminated": False, "error": str(e)}

    @staticmethod
    def _remove_session_dir_sync(path: Path) -> dict:
        size_bytes = MaxHygieneManager._session_dir_size_bytes(path)
        if not path.exists():
            return {"path": str(path), "removed": True, "already_missing": True, "size_bytes": 0}
        try:
            shutil.rmtree(path)
            return {"path": str(path), "removed": True, "size_bytes": size_bytes}
        except Exception as e:
            return {"path": str(path), "removed": False, "size_bytes": 0, "error": str(e)}

    def _record_cleanup_result(self, summary: dict, events: list[dict]) -> None:
        self.last_run_at = time.time()
        self.last_summary = summary
        for row in events:
            if isinstance(row, dict):
                event = dict(row)
                event.setdefault("timestamp", self.last_run_at)
                self._events.append(event)
        self._persist_report_sync()

    async def close_max_system_sessions(
        self,
        *,
        target: str = "stale",
        pids: list[int] | None = None,
        force: bool = True,
        dry_run: bool = False,
        max_count: int = 0,
    ) -> dict:
        allowed_targets = {"all", "stale", "managed", "custom"}
        target_normalized = target.strip().lower()
        if target_normalized not in allowed_targets:
            return _error_result(
                ERROR_VALIDATION,
                f"target must be one of {sorted(allowed_targets)}",
                recoverable=True,
            )
        inventory = await self.list_system_sessions(include_windows=False, include_runtime_state=True)
        discovered = inventory.get("max_processes", [])
        if not isinstance(discovered, list):
            discovered = []

        selected_rows: list[dict] = []
        if target_normalized == "all":
            selected_rows = [row for row in discovered if isinstance(row, dict)]
        elif target_normalized == "stale":
            selected_rows = [
                row
                for row in discovered
                if isinstance(row, dict) and bool(row.get("is_stale"))
            ]
        elif target_normalized == "managed":
            selected_rows = [
                row
                for row in discovered
                if isinstance(row, dict)
                and str(row.get("classified_as", "")).startswith("managed_")
            ]
        else:
            requested = []
            for pid in pids or []:
                try:
                    requested.append(int(pid))
                except Exception:
                    continue
            index = {
                int(row.get("pid")): row
                for row in discovered
                if isinstance(row, dict) and isinstance(row.get("pid"), int)
            }
            selected_rows = [
                index.get(pid, {"pid": pid, "classified_as": "custom_pid", "is_stale": None})
                for pid in requested
            ]

        if self.scope == "managed_only":
            selected_rows = [
                row
                for row in selected_rows
                if str(row.get("classified_as", "")).startswith("managed_")
            ]
        if max_count and max_count > 0:
            selected_rows = selected_rows[: max_count]

        killed: list[dict] = []
        failed: list[dict] = []
        skipped: list[dict] = []
        event_rows: list[dict] = []
        for row in selected_rows:
            pid = row.get("pid")
            if not isinstance(pid, int) or pid <= 1:
                skipped.append({"pid": pid, "reason": "invalid_pid"})
                continue
            if dry_run:
                killed.append(
                    {
                        "pid": pid,
                        "dry_run": True,
                        "classified_as": row.get("classified_as"),
                        "is_stale": row.get("is_stale"),
                    }
                )
                continue
            result = await asyncio.to_thread(self._kill_pid_sync, pid, force)
            result["classified_as"] = row.get("classified_as")
            result["is_stale"] = row.get("is_stale")
            if result.get("terminated"):
                killed.append(result)
                event_rows.append(
                    {
                        "action": "kill_process",
                        "pid": pid,
                        "result": "terminated",
                        "classified_as": row.get("classified_as"),
                        "is_stale": row.get("is_stale"),
                    }
                )
            else:
                failed.append(result)
                event_rows.append(
                    {
                        "action": "kill_process",
                        "pid": pid,
                        "result": "failed",
                        "classified_as": row.get("classified_as"),
                        "error": result.get("error"),
                    }
                )

        summary = {
            "operation": "close_max_system_sessions",
            "target": target_normalized,
            "scope": self.scope,
            "dry_run": dry_run,
            "requested": len(selected_rows),
            "terminated": len([row for row in killed if not row.get("dry_run")]),
            "failed": len(failed),
            "skipped": len(skipped),
        }
        if not dry_run:
            await asyncio.to_thread(self._record_cleanup_result, summary, event_rows)
        return {
            "success": True,
            "summary": summary,
            "actions_taken": killed,
            "actions_failed": failed,
            "skipped": skipped,
            "inventory_summary": inventory.get("summary", {}),
        }

    async def cleanup_hygiene(
        self,
        *,
        mode: str = "aggressive",
        include_processes: bool = True,
        include_session_dirs: bool = True,
        dry_run: bool = False,
        reason: str = "manual",
    ) -> dict:
        mode_normalized = mode.strip().lower()
        if mode_normalized not in {"aggressive", "preview"}:
            return _error_result(
                ERROR_VALIDATION,
                "mode must be one of: aggressive, preview.",
                recoverable=True,
            )
        async with self._lock:
            inventory = await self.list_system_sessions(
                include_windows=False,
                include_runtime_state=True,
            )
            processes = inventory.get("max_processes", [])
            sessions = inventory.get("managed_sessions_on_disk", [])
            if not isinstance(processes, list):
                processes = []
            if not isinstance(sessions, list):
                sessions = []

            killed: list[dict] = []
            deleted_sessions: list[dict] = []
            failed: list[dict] = []
            skipped: list[dict] = []
            event_rows: list[dict] = []

            if include_processes:
                process_candidates = [
                    row
                    for row in processes
                    if isinstance(row, dict) and bool(row.get("is_stale"))
                ]
                if self.scope == "managed_only":
                    process_candidates = [
                        row
                        for row in process_candidates
                        if str(row.get("classified_as", "")).startswith("managed_")
                    ]
                process_candidates = process_candidates[: self.max_kills_per_sweep]
                for row in process_candidates:
                    pid = row.get("pid")
                    if not isinstance(pid, int) or pid <= 1:
                        skipped.append({"pid": pid, "reason": "invalid_pid"})
                        continue
                    if dry_run or mode_normalized == "preview":
                        killed.append(
                            {
                                "pid": pid,
                                "dry_run": True,
                                "classified_as": row.get("classified_as"),
                                "is_stale": row.get("is_stale"),
                            }
                        )
                        continue
                    result = await asyncio.to_thread(self._kill_pid_sync, pid, True)
                    result["classified_as"] = row.get("classified_as")
                    result["is_stale"] = row.get("is_stale")
                    if result.get("terminated"):
                        killed.append(result)
                        event_rows.append(
                            {
                                "action": "kill_process",
                                "pid": pid,
                                "result": "terminated",
                                "classified_as": row.get("classified_as"),
                            }
                        )
                    else:
                        failed.append(result)
                        event_rows.append(
                            {
                                "action": "kill_process",
                                "pid": pid,
                                "result": "failed",
                                "error": result.get("error"),
                            }
                        )

            reclaimed_bytes = 0
            if include_session_dirs:
                current_session = self.runtime.session_id
                keep_recent = set()
                for row in sessions[: self.keep_recent_sessions]:
                    session_id = row.get("session_id")
                    if isinstance(session_id, str) and session_id:
                        keep_recent.add(session_id)
                for row in sessions:
                    if not isinstance(row, dict):
                        continue
                    session_id = row.get("session_id")
                    path = row.get("path")
                    if not isinstance(session_id, str) or not isinstance(path, str):
                        continue
                    if session_id == current_session:
                        skipped.append({"session_id": session_id, "reason": "current_session"})
                        continue
                    if session_id in keep_recent:
                        skipped.append({"session_id": session_id, "reason": "recent_retention"})
                        continue
                    if not bool(row.get("is_stale")):
                        continue
                    if dry_run or mode_normalized == "preview":
                        deleted_sessions.append(
                            {
                                "session_id": session_id,
                                "path": path,
                                "dry_run": True,
                                "size_bytes": row.get("size_bytes", 0),
                            }
                        )
                        continue
                    result = await asyncio.to_thread(self._remove_session_dir_sync, Path(path))
                    result["session_id"] = session_id
                    if result.get("removed"):
                        deleted_sessions.append(result)
                        reclaimed_bytes += int(result.get("size_bytes", 0) or 0)
                        event_rows.append(
                            {
                                "action": "delete_session_dir",
                                "session_id": session_id,
                                "path": path,
                                "result": "removed",
                                "size_bytes": int(result.get("size_bytes", 0) or 0),
                            }
                        )
                    else:
                        failed.append(result)
                        event_rows.append(
                            {
                                "action": "delete_session_dir",
                                "session_id": session_id,
                                "path": path,
                                "result": "failed",
                                "error": result.get("error"),
                            }
                        )

            summary = {
                "operation": "cleanup_max_hygiene",
                "reason": reason,
                "scope": self.scope,
                "mode": mode_normalized,
                "dry_run": dry_run,
                "processes_terminated": len([row for row in killed if not row.get("dry_run")]),
                "sessions_deleted": len([row for row in deleted_sessions if not row.get("dry_run")]),
                "failed": len(failed),
                "skipped": len(skipped),
                "reclaimed_bytes": reclaimed_bytes,
            }
            if not dry_run and mode_normalized != "preview":
                await asyncio.to_thread(self._record_cleanup_result, summary, event_rows)
            return {
                "success": True,
                "summary": summary,
                "actions_taken": {
                    "processes": killed,
                    "session_dirs": deleted_sessions,
                },
                "actions_failed": failed,
                "skipped": skipped,
                "inventory_summary": inventory.get("summary", {}),
            }

    async def run_automatic_cleanup(self, trigger: str = "periodic") -> dict:
        if not self.auto_cleanup:
            return {"success": True, "skipped": True, "reason": "auto_cleanup_disabled"}
        return await self.cleanup_hygiene(
            mode=self.mode,
            include_processes=True,
            include_session_dirs=True,
            dry_run=False,
            reason=trigger,
        )

    async def run_startup_cleanup_once(self) -> dict | None:
        if not self.startup_sweep:
            return None
        async with self._lock:
            if self._startup_cleanup_ran:
                return None
            self._startup_cleanup_ran = True
        return await self.run_automatic_cleanup(trigger="startup")

    def get_report(self, limit: int = 100) -> dict:
        bounded = max(1, int(limit))
        return {
            "policy": self.policy_snapshot(),
            "last_run_at": self.last_run_at,
            "last_summary": self.last_summary,
            "events": list(self._events)[-bounded:],
            "report_file": str(self.report_file),
        }


def _is_protected_varname(varname: str) -> bool:
    return bool(varname and varname.startswith(PROTECTED_VARNAME_PREFIX))


def _protected_varname_error(varname: str) -> dict:
    return _error_result(
        ERROR_PROTECTED_OBJECT,
        f"PROTECTED OBJECT: '{varname}' belongs to the managed bridge. "
        "Mutating bridge objects is blocked to keep MCP connectivity alive.",
        recoverable=False,
    )


async def _bridge_heartbeat_loop(maxmsp: MaxMSPConnection):
    while True:
        await asyncio.sleep(maxmsp.heartbeat_interval_seconds)
        if not maxmsp.sio.connected:
            continue
        await maxmsp.ping_bridge(timeout=2.0)


async def _bridge_metrics_loop(maxmsp: MaxMSPConnection):
    while True:
        await asyncio.sleep(maxmsp.metrics_log_interval_seconds)
        try:
            maxmsp.emit_metrics_log()
        except Exception as e:
            logging.warning(f"Bridge metrics loop error: {e}")


async def _hygiene_loop(hygiene: MaxHygieneManager):
    while True:
        await asyncio.sleep(hygiene.loop_interval_seconds)
        try:
            await hygiene.run_automatic_cleanup(trigger="periodic")
        except Exception as e:
            logging.warning(f"Hygiene loop error: {e}")


@asynccontextmanager
async def server_lifespan(server: FastMCP):
    """Manage server lifespan"""
    maxmsp = MaxMSPConnection(SOCKETIO_SERVER_URL, SOCKETIO_SERVER_PORT, NAMESPACE)
    runtime = MaxRuntimeManager(maxmsp)
    hygiene = MaxHygieneManager(runtime, maxmsp)
    maxmsp.runtime_manager = runtime
    runtime.hygiene_manager = hygiene
    heartbeat_task = asyncio.create_task(_bridge_heartbeat_loop(maxmsp))
    metrics_task = asyncio.create_task(_bridge_metrics_loop(maxmsp))
    hygiene_task = asyncio.create_task(_hygiene_loop(hygiene))
    try:
        status = await runtime.ensure_runtime_ready()
        if status.get("ready"):
            logging.info(f"Connected to MaxMSP bridge at {maxmsp.endpoint}")
        else:
            logging.warning(
                f"Starting in degraded mode. {status.get('error', maxmsp._offline_error_message())}"
            )
        # Yield the Socket.IO connection and runtime manager to lifespan context
        yield {"maxmsp": maxmsp, "runtime": runtime, "hygiene": hygiene}
    finally:
        logging.info("Shutting down connection")
        maxmsp.emit_metrics_log(force=True)
        heartbeat_task.cancel()
        metrics_task.cancel()
        hygiene_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await metrics_task
        except asyncio.CancelledError:
            pass
        try:
            await hygiene_task
        except asyncio.CancelledError:
            pass
        await maxmsp.disconnect()


# Create the MCP server with lifespan support
mcp = FastMCP(
    "MaxMSPMCP",
    description="MaxMSP integration through the Model Context Protocol",
    lifespan=server_lifespan,
)


def _get_runtime(ctx: Context) -> MaxRuntimeManager | None:
    return ctx.request_context.lifespan_context.get("runtime")


def _get_hygiene(ctx: Context) -> MaxHygieneManager | None:
    return ctx.request_context.lifespan_context.get("hygiene")


@mcp.tool()
async def ensure_max_available(ctx: Context) -> dict:
    """Ensure Max and the managed bridge patch are available.

    This tool is safe to call before any other operation. In managed mode it:
    - installs Node dependencies if needed
    - launches Max with the managed host patch
    - waits for bridge connection
    """
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"ready": False, "error": "Runtime manager is unavailable."}
    status = await runtime.ensure_runtime_ready()
    return status


@mcp.tool()
async def bridge_status(ctx: Context, verbose: bool = False) -> dict:
    """Get current bridge/runtime health and setup details."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"ready": False, "error": "Runtime manager is unavailable."}
    status = await runtime.collect_status(check_bridge=True)
    if verbose:
        return status
    return {
        "ready": bool(status.get("bridge_connected")),
        "managed_mode": status.get("managed_mode"),
        "bridge_connected": status.get("bridge_connected"),
        "bridge_healthy": status.get("bridge_healthy"),
        "active_target": status.get("active_target"),
        "host_patch_path": status.get("host_patch_path"),
        "node_modules_ready": status.get("node_modules_ready"),
    }


@mcp.tool()
def get_bridge_health(ctx: Context) -> dict:
    """Return a compact health snapshot for the Max bridge connection."""
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    if maxmsp is None:
        return _error_result(
            ERROR_BRIDGE_UNAVAILABLE,
            "Bridge connection is unavailable in this MCP lifespan context.",
            recoverable=True,
        )

    health = maxmsp.health_snapshot()
    return {
        "connected": health["connected"],
        "stale": health["stale"],
        "response_age_seconds": health["response_age_seconds"],
        "total_requests": health["total_requests"],
        "total_failures": health["total_failures"],
        "total_timeouts": health["total_timeouts"],
        "consecutive_failures": health["consecutive_failures"],
        "protocol_version": health["protocol_version"],
    }


@mcp.tool()
def get_bridge_diagnostics(ctx: Context) -> dict:
    """Return full diagnostics: health counters, capabilities, and endpoint metadata."""
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    if maxmsp is None:
        return _error_result(
            ERROR_BRIDGE_UNAVAILABLE,
            "Bridge connection is unavailable in this MCP lifespan context.",
            recoverable=True,
        )
    diagnostics = maxmsp.health_snapshot()
    diagnostics["metrics"] = maxmsp.metrics_snapshot(include_events=False)
    return diagnostics


@mcp.tool()
def get_bridge_metrics(
    ctx: Context,
    include_events: bool = False,
    event_limit: int = 25,
) -> dict:
    """Return bridge performance metrics, queue pressure, and optional recent events."""
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    if maxmsp is None:
        return _error_result(
            ERROR_BRIDGE_UNAVAILABLE,
            "Bridge connection is unavailable in this MCP lifespan context.",
            recoverable=True,
        )
    return maxmsp.metrics_snapshot(
        include_events=include_events,
        event_limit=event_limit,
    )


@mcp.tool()
async def recover_bridge(ctx: Context) -> dict:
    """Reconnect or relaunch managed bridge runtime."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"ready": False, "error": "Runtime manager is unavailable."}
    return await runtime.recover_bridge()


@mcp.tool()
async def list_max_system_sessions(
    ctx: Context,
    include_windows: bool = True,
    include_runtime_state: bool = True,
) -> dict:
    """List system-wide Max processes, open patch windows, and managed session artifacts."""
    hygiene = _get_hygiene(ctx)
    if hygiene is None:
        return {"success": False, "error": "Hygiene manager is unavailable."}
    inventory = await hygiene.list_system_sessions(
        include_windows=include_windows,
        include_runtime_state=include_runtime_state,
    )
    inventory["success"] = True
    return inventory


@mcp.tool()
async def close_max_system_sessions(
    ctx: Context,
    target: str = "stale",
    pids: list[int] | None = None,
    force: bool = True,
    dry_run: bool = False,
    max_count: int = 0,
) -> dict:
    """Close/terminate Max sessions by selector (all/stale/managed/custom)."""
    hygiene = _get_hygiene(ctx)
    if hygiene is None:
        return {"success": False, "error": "Hygiene manager is unavailable."}
    return await hygiene.close_max_system_sessions(
        target=target,
        pids=pids,
        force=force,
        dry_run=dry_run,
        max_count=max_count,
    )


@mcp.tool()
async def cleanup_max_hygiene(
    ctx: Context,
    mode: str = "aggressive",
    include_processes: bool = True,
    include_session_dirs: bool = True,
    dry_run: bool = False,
) -> dict:
    """Run Max resource hygiene cleanup (stale processes + stale managed session artifacts)."""
    hygiene = _get_hygiene(ctx)
    if hygiene is None:
        return {"success": False, "error": "Hygiene manager is unavailable."}
    return await hygiene.cleanup_hygiene(
        mode=mode,
        include_processes=include_processes,
        include_session_dirs=include_session_dirs,
        dry_run=dry_run,
        reason="manual",
    )


@mcp.tool()
def get_hygiene_report(ctx: Context, limit: int = 100) -> dict:
    """Return recent cleanup report entries and effective hygiene policy."""
    hygiene = _get_hygiene(ctx)
    if hygiene is None:
        return {"success": False, "error": "Hygiene manager is unavailable."}
    report = hygiene.get_report(limit=limit)
    report["success"] = True
    return report


@mcp.tool()
def set_hygiene_policy(
    ctx: Context,
    auto_cleanup: bool = True,
    scope: str = "all_max_instances",
    mode: str = "aggressive",
    stale_seconds: int = 1800,
    startup_sweep: bool = True,
) -> dict:
    """Set in-memory hygiene policy for this MCP server process."""
    hygiene = _get_hygiene(ctx)
    if hygiene is None:
        return {"success": False, "error": "Hygiene manager is unavailable."}
    try:
        policy = hygiene.set_policy(
            auto_cleanup=auto_cleanup,
            scope=scope,
            mode=mode,
            stale_seconds=stale_seconds,
            startup_sweep=startup_sweep,
        )
    except MaxMCPError as e:
        return {"success": False, "error": e.to_dict()}
    return {"success": True, "policy": policy}


@mcp.tool()
def list_patch_targets(ctx: Context) -> list:
    """List patch targets exposed by the managed runtime."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return []
    return runtime.list_patch_targets()


@mcp.tool()
async def set_patch_target(ctx: Context, target: str = "active") -> dict:
    """Set logical patch target.

    `active` and `scratch` map to per-session managed workspaces inside the host patch.
    """
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"success": False, "error": "Runtime manager is unavailable."}
    return await runtime.set_active_target(target)


@mcp.tool()
async def validate_patch_file(ctx: Context, path: str, strict: bool = False) -> dict:
    """Validate and summarize a .maxpat/.json patch file without mutating runtime state."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"success": False, "error": "Runtime manager is unavailable."}
    return await runtime.validate_patch_file(path=path, strict=strict)


@mcp.tool()
async def load_patch_from_path(
    ctx: Context,
    path: str,
    target: str = "active",
    mode: str = "replace",
    auto_rename_collisions: bool = True,
    create_checkpoint_before_load: bool = True,
    checkpoint_label: str = "pre_load_import",
    idempotency_key: str = "",
) -> dict:
    """Load a patch file from disk directly into a managed workspace target."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"success": False, "error": "Runtime manager is unavailable."}
    return await runtime.load_patch_from_path(
        path=path,
        target=target,
        mode=mode,
        auto_rename_collisions=auto_rename_collisions,
        create_checkpoint_before_load=create_checkpoint_before_load,
        checkpoint_label=checkpoint_label,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
async def save_patch_to_path(
    ctx: Context,
    path: str,
    target: str = "",
    overwrite: bool = False,
) -> dict:
    """Export the current managed workspace topology to a .maxpat/.json path."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"success": False, "error": "Runtime manager is unavailable."}
    return await runtime.save_patch_to_path(
        path=path,
        target=target,
        overwrite=overwrite,
    )


@mcp.tool()
async def sync_patch_twin(ctx: Context, reason: str = "manual") -> dict:
    """Synchronize the in-memory patch twin with the live patch topology."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"success": False, "error": "Runtime manager is unavailable."}
    return await runtime.sync_patch_twin(reason=reason)


@mcp.tool()
async def get_patch_drift(ctx: Context, auto_resync: bool = False) -> dict:
    """Check if live patch topology drifted from the in-memory patch twin baseline."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"success": False, "error": "Runtime manager is unavailable."}
    return await runtime.check_patch_drift(auto_resync=auto_resync)


@mcp.tool()
async def create_checkpoint(ctx: Context, label: str = "") -> dict:
    """Create a topology checkpoint for rollback/restore."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"success": False, "error": "Runtime manager is unavailable."}
    return await runtime.create_checkpoint(label=label)


@mcp.tool()
def list_checkpoints(ctx: Context) -> list:
    """List available topology checkpoints (newest first)."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return []
    return runtime.list_checkpoints()


@mcp.tool()
async def restore_checkpoint(ctx: Context, checkpoint_id: str) -> dict:
    """Restore a previously captured topology checkpoint."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"success": False, "error": "Runtime manager is unavailable."}
    return await runtime.restore_checkpoint(checkpoint_id=checkpoint_id)


@mcp.tool()
async def dry_run_plan(
    ctx: Context,
    steps: list,
    engine: str = "basic",
    unknown_action_policy: str = "error",
) -> dict:
    """Validate a planned sequence of patch mutations without applying any change."""
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    starting_context = {"depth": 0, "path": [], "is_root": True}
    topology = None
    if maxmsp is not None:
        try:
            context_response = await maxmsp.send_request(
                {"action": "get_patcher_context"},
                timeout=2.0,
            )
            if isinstance(context_response, dict):
                starting_context = context_response
        except Exception:
            # Keep dry-run usable even when offline by running static validation only.
            pass
        if engine == "maxpy":
            try:
                topology = await maxmsp.send_request(
                    {"action": "get_objects_in_patch"},
                    timeout=4.0,
                )
            except Exception:
                topology = None

    errors: list[dict] = []
    warnings: list[dict] = []
    normalized_steps: list[dict] = []
    virtual_depth = int(starting_context.get("depth", 0) or 0)
    engine_mode = engine.strip().lower()
    if engine_mode not in {"basic", "maxpy"}:
        warnings.append(
            {
                "step": 0,
                "code": ERROR_VALIDATION,
                "message": f"Unknown engine '{engine}'. Falling back to 'basic'.",
            }
        )
        engine_mode = "basic"
    unknown_policy = unknown_action_policy.strip().lower()
    if unknown_policy not in {"error", "warn"}:
        warnings.append(
            {
                "step": 0,
                "code": ERROR_VALIDATION,
                "message": (
                    f"Unknown unknown_action_policy '{unknown_action_policy}'. "
                    "Falling back to 'error'."
                ),
            }
        )
        unknown_policy = "error"

    virtual_objects: dict[str, dict] = {}
    virtual_connections: set[tuple[str, int, str, int]] = set()
    if isinstance(topology, dict):
        for row in topology.get("boxes", []):
            if not isinstance(row, dict):
                continue
            box = row.get("box", {})
            if not isinstance(box, dict):
                continue
            varname = box.get("varname")
            if not isinstance(varname, str) or not varname:
                continue
            virtual_objects[varname] = {
                "obj_type": box.get("maxclass", "unknown"),
                "numinlets": box.get("numinlets"),
                "numoutlets": box.get("numoutlets"),
            }
        for line in topology.get("lines", []):
            if not isinstance(line, dict):
                continue
            patchline = line.get("patchline", {})
            if not isinstance(patchline, dict):
                continue
            source = patchline.get("source", [])
            destination = patchline.get("destination", [])
            if (
                isinstance(source, list)
                and len(source) >= 2
                and isinstance(destination, list)
                and len(destination) >= 2
                and isinstance(source[0], str)
                and isinstance(destination[0], str)
                and isinstance(source[1], int)
                and isinstance(destination[1], int)
            ):
                virtual_connections.add((source[0], source[1], destination[0], destination[1]))

    def _parse_index(value: Any, field: str, step_idx: int) -> int | None:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        errors.append(
            {
                "step": step_idx,
                "code": ERROR_VALIDATION,
                "message": f"{field} must be an integer.",
            }
        )
        return None

    def _validate_connection_endpoints(
        *,
        src_varname: str,
        outlet_idx: int | None,
        dst_varname: str,
        inlet_idx: int | None,
        step_idx: int,
    ) -> None:
        if outlet_idx is None or inlet_idx is None:
            return
        if outlet_idx < 0 or inlet_idx < 0:
            errors.append(
                {
                    "step": step_idx,
                    "code": ERROR_VALIDATION,
                    "message": "Outlet and inlet indices must be >= 0.",
                }
            )
            return

        src_info = virtual_objects.get(src_varname)
        dst_info = virtual_objects.get(dst_varname)
        if src_info is None:
            errors.append(
                {
                    "step": step_idx,
                    "code": ERROR_OBJECT_NOT_FOUND,
                    "message": f"Source object not found: {src_varname}",
                }
            )
        if dst_info is None:
            errors.append(
                {
                    "step": step_idx,
                    "code": ERROR_OBJECT_NOT_FOUND,
                    "message": f"Destination object not found: {dst_varname}",
                }
            )
        if src_info is None or dst_info is None:
            return

        src_outs = src_info.get("numoutlets")
        dst_ins = dst_info.get("numinlets")
        if isinstance(src_outs, int) and outlet_idx >= src_outs:
            errors.append(
                {
                    "step": step_idx,
                    "code": ERROR_VALIDATION,
                    "message": (
                        f"Outlet index out of range for {src_varname}: {outlet_idx} "
                        f"(numoutlets={src_outs})"
                    ),
                }
            )
        if isinstance(dst_ins, int) and inlet_idx >= dst_ins:
            errors.append(
                {
                    "step": step_idx,
                    "code": ERROR_VALIDATION,
                    "message": (
                        f"Inlet index out of range for {dst_varname}: {inlet_idx} "
                        f"(numinlets={dst_ins})"
                    ),
                }
            )

    for index, raw_step in enumerate(steps):
        step_idx = index + 1
        if not isinstance(raw_step, dict):
            errors.append(
                {
                    "step": step_idx,
                    "code": ERROR_VALIDATION,
                    "message": "Each step must be an object with action and params keys.",
                }
            )
            continue

        action = raw_step.get("action")
        params = raw_step.get("params", {})
        if not isinstance(params, dict):
            errors.append(
                {
                    "step": step_idx,
                    "code": ERROR_VALIDATION,
                    "message": "Step params must be an object.",
                }
            )
            continue

        if not isinstance(action, str) or not action:
            errors.append(
                {
                    "step": step_idx,
                    "code": ERROR_VALIDATION,
                    "message": "Missing or invalid action.",
                }
            )
            continue

        if action == "add_max_object":
            required = {"position", "obj_type", "varname", "args"}
            missing = [field for field in required if field not in params]
            if missing:
                errors.append(
                    {
                        "step": step_idx,
                        "code": ERROR_VALIDATION,
                        "message": f"Missing fields for add_max_object: {missing}",
                    }
                )
            else:
                validation_error = _validate_add_max_object_payload(
                    obj_type=params["obj_type"],
                    args=params["args"],
                    int_mode=bool(params.get("int_mode", False)),
                    extend=bool(params.get("extend", False)),
                    use_live_dial=bool(params.get("use_live_dial", False)),
                    trigger_rtl=bool(params.get("trigger_rtl", False)),
                )
                if validation_error:
                    errors.append({"step": step_idx, **validation_error["error"]})

                if engine_mode == "maxpy":
                    obj_type = str(params["obj_type"])
                    canonical_name, via_alias = maxpy_catalog.resolve_name(obj_type)
                    varname = str(params["varname"])
                    if varname in virtual_objects:
                        errors.append(
                            {
                                "step": step_idx,
                                "code": ERROR_VALIDATION,
                                "message": f"Duplicate varname in virtual graph: {varname}",
                            }
                        )
                    if via_alias:
                        warnings.append(
                            {
                                "step": step_idx,
                                "code": ERROR_VALIDATION,
                                "message": (
                                    f"Object alias '{obj_type}' resolved to '{canonical_name}' "
                                    "during dry-run."
                                ),
                            }
                        )

                    num_inlets = None
                    num_outlets = None
                    schema = maxpy_catalog.get_schema(canonical_name)
                    if schema:
                        args_info = schema.get("schema", {}).get("args", {})
                        required_args = args_info.get("required", []) if isinstance(args_info, dict) else []
                        if isinstance(required_args, list) and len(params["args"]) < len(required_args):
                            errors.append(
                                {
                                    "step": step_idx,
                                    "code": ERROR_VALIDATION,
                                    "message": (
                                        f"Too few arguments for '{canonical_name}': got {len(params['args'])}, "
                                        f"requires at least {len(required_args)}."
                                    ),
                                }
                            )
                        num_inlets, num_outlets = maxpy_catalog.io_counts(canonical_name)
                    else:
                        suggestions = maxpy_catalog.suggest(canonical_name)
                        warnings.append(
                            {
                                "step": step_idx,
                                "code": ERROR_OBJECT_NOT_FOUND,
                                "message": (
                                    f"Object '{canonical_name}' not found in MaxPyLang metadata. "
                                    "It may be an abstraction/external."
                                ),
                                "suggestions": suggestions,
                            }
                        )

                    virtual_objects[varname] = {
                        "obj_type": canonical_name,
                        "numinlets": num_inlets,
                        "numoutlets": num_outlets,
                    }

        elif action in {"remove_max_object", "send_bang_to_object", "autofit_existing"}:
            if "varname" not in params:
                errors.append(
                    {
                        "step": step_idx,
                        "code": ERROR_VALIDATION,
                        "message": f"{action} requires 'varname'.",
                    }
                )
            elif engine_mode == "maxpy":
                varname = str(params["varname"])
                if varname not in virtual_objects:
                    errors.append(
                        {
                            "step": step_idx,
                            "code": ERROR_OBJECT_NOT_FOUND,
                            "message": f"Object not found in virtual graph: {varname}",
                        }
                    )
                if action == "remove_max_object" and varname in virtual_objects:
                    virtual_objects.pop(varname, None)
                    virtual_connections = {
                        conn
                        for conn in virtual_connections
                        if conn[0] != varname and conn[2] != varname
                    }

        elif action in {"connect_max_objects", "disconnect_max_objects"}:
            required = {"src_varname", "outlet_idx", "dst_varname", "inlet_idx"}
            missing = [field for field in required if field not in params]
            if missing:
                errors.append(
                    {
                        "step": step_idx,
                        "code": ERROR_VALIDATION,
                        "message": f"{action} missing fields: {missing}",
                    }
                )
            elif engine_mode == "maxpy":
                src_varname = str(params["src_varname"])
                dst_varname = str(params["dst_varname"])
                outlet_idx = _parse_index(params["outlet_idx"], "outlet_idx", step_idx)
                inlet_idx = _parse_index(params["inlet_idx"], "inlet_idx", step_idx)
                _validate_connection_endpoints(
                    src_varname=src_varname,
                    outlet_idx=outlet_idx,
                    dst_varname=dst_varname,
                    inlet_idx=inlet_idx,
                    step_idx=step_idx,
                )
                if outlet_idx is not None and inlet_idx is not None:
                    connection = (src_varname, outlet_idx, dst_varname, inlet_idx)
                    if action == "connect_max_objects":
                        if connection in virtual_connections:
                            warnings.append(
                                {
                                    "step": step_idx,
                                    "code": ERROR_PRECONDITION,
                                    "message": "Connection already exists in virtual graph.",
                                }
                            )
                        virtual_connections.add(connection)
                    else:
                        if connection not in virtual_connections:
                            warnings.append(
                                {
                                    "step": step_idx,
                                    "code": ERROR_PRECONDITION,
                                    "message": "Connection did not exist in virtual graph.",
                                }
                            )
                        virtual_connections.discard(connection)

        elif action == "enter_subpatcher":
            if "varname" not in params:
                errors.append(
                    {
                        "step": step_idx,
                        "code": ERROR_VALIDATION,
                        "message": "enter_subpatcher requires 'varname'.",
                    }
                )
            else:
                virtual_depth += 1

        elif action == "exit_subpatcher":
            if virtual_depth == 0:
                warnings.append(
                    {
                        "step": step_idx,
                        "code": ERROR_PRECONDITION,
                        "message": "exit_subpatcher called at root depth; this is a no-op in Max.",
                    }
                )
            else:
                virtual_depth -= 1

        elif action in {
            "set_object_attribute",
            "set_message_text",
            "send_messages_to_object",
            "set_number",
            "create_subpatcher",
            "add_subpatcher_io",
            "recreate_with_args",
            "move_object",
            "encapsulate",
            "check_signal_safety",
            "get_patch_context",
            "get_objects_in_patch",
            "get_objects_in_selected",
            "get_object_attributes",
            "get_avoid_rect_position",
            "get_patcher_context",
            "get_object_connections",
        }:
            # Supported action with simple shape accepted; detailed checks happen during execution.
            pass
        else:
            payload = {
                "step": step_idx,
                "code": ERROR_UNKNOWN_ACTION,
                "message": f"Action '{action}' is not recognized by dry_run_plan.",
            }
            if unknown_policy == "warn":
                warnings.append(payload)
            else:
                errors.append(payload)

        normalized_steps.append({"step": step_idx, "action": action, "params": params})

    return {
        "valid": len(errors) == 0,
        "engine": engine_mode,
        "unknown_action_policy": unknown_policy,
        "steps_analyzed": len(steps),
        "starting_context": starting_context,
        "ending_virtual_depth": virtual_depth,
        "errors": errors,
        "warnings": warnings,
        "normalized_steps": normalized_steps,
        "maxpy": {
            "catalog_available": maxpy_catalog.available,
            "schema_hash": maxpy_catalog.schema_hash,
            "virtual_object_count": len(virtual_objects) if engine_mode == "maxpy" else 0,
            "virtual_connection_count": len(virtual_connections) if engine_mode == "maxpy" else 0,
            "live_topology_loaded": bool(isinstance(topology, dict)),
        },
    }


def _build_transaction_bridge_request(step_idx: int, action: str, params: dict) -> tuple[dict, float]:
    """Translate a dry_run-style step action into a bridge request payload."""
    if action == "add_max_object":
        required = {"position", "obj_type", "varname", "args"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: add_max_object missing fields: {missing}",
                recoverable=False,
            )
        if _is_protected_varname(str(params["varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{params['varname']}' cannot be mutated.",
                recoverable=False,
            )
        validation_error = _validate_add_max_object_payload(
            obj_type=str(params["obj_type"]),
            args=list(params["args"]),
            int_mode=bool(params.get("int_mode", False)),
            extend=bool(params.get("extend", False)),
            use_live_dial=bool(params.get("use_live_dial", False)),
            trigger_rtl=bool(params.get("trigger_rtl", False)),
        )
        if validation_error:
            err = validation_error.get("error", {})
            raise MaxMCPError(
                err.get("code", ERROR_VALIDATION),
                f"Step {step_idx}: {err.get('message', 'Validation failed')}",
                hint=err.get("hint"),
                recoverable=bool(err.get("recoverable", False)),
                details=err.get("details") if isinstance(err.get("details"), dict) else {},
            )
        return (
            {
                "action": "add_object",
                "position": params["position"],
                "obj_type": params["obj_type"],
                "args": _convert_string_args(list(params["args"])),
                "varname": params["varname"],
            },
            8.0,
        )

    if action in {"remove_max_object", "send_bang_to_object", "autofit_existing"}:
        varname = params.get("varname")
        if not isinstance(varname, str) or not varname:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: {action} requires 'varname'.",
                recoverable=False,
            )
        if _is_protected_varname(varname):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{varname}' cannot be mutated.",
                recoverable=False,
            )
        bridge_action = {
            "remove_max_object": "remove_object",
            "send_bang_to_object": "send_bang_to_object",
            "autofit_existing": "autofit_existing",
        }[action]
        return ({"action": bridge_action, "varname": varname}, 5.0)

    if action in {"connect_max_objects", "disconnect_max_objects"}:
        required = {"src_varname", "outlet_idx", "dst_varname", "inlet_idx"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: {action} missing fields: {missing}",
                recoverable=False,
            )
        src = str(params["src_varname"])
        dst = str(params["dst_varname"])
        if _is_protected_varname(src) or _is_protected_varname(dst):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname involved in connection operation.",
                recoverable=False,
            )
        bridge_action = "connect_objects" if action == "connect_max_objects" else "disconnect_objects"
        return (
            {
                "action": bridge_action,
                "src_varname": src,
                "outlet_idx": params["outlet_idx"],
                "dst_varname": dst,
                "inlet_idx": params["inlet_idx"],
            },
            5.0,
        )

    if action == "set_object_attribute":
        required = {"varname", "attr_name", "attr_value"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: set_object_attribute missing fields: {missing}",
                recoverable=False,
            )
        if _is_protected_varname(str(params["varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{params['varname']}' cannot be mutated.",
                recoverable=False,
            )
        return (
            {
                "action": "set_object_attribute",
                "varname": params["varname"],
                "attr_name": params["attr_name"],
                "attr_value": params["attr_value"],
            },
            5.0,
        )

    if action == "set_message_text":
        required = {"varname", "text_list"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: set_message_text missing fields: {missing}",
                recoverable=False,
            )
        if _is_protected_varname(str(params["varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{params['varname']}' cannot be mutated.",
                recoverable=False,
            )
        return (
            {
                "action": "set_message_text",
                "varname": params["varname"],
                "new_text": params["text_list"],
            },
            5.0,
        )

    if action == "send_messages_to_object":
        required = {"varname", "message"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: send_messages_to_object missing fields: {missing}",
                recoverable=False,
            )
        if _is_protected_varname(str(params["varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{params['varname']}' cannot be mutated.",
                recoverable=False,
            )
        return (
            {
                "action": "send_message_to_object",
                "varname": params["varname"],
                "message": params["message"],
            },
            5.0,
        )

    if action == "set_number":
        required = {"varname", "num"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: set_number missing fields: {missing}",
                recoverable=False,
            )
        if _is_protected_varname(str(params["varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{params['varname']}' cannot be mutated.",
                recoverable=False,
            )
        return (
            {"action": "set_number", "varname": params["varname"], "num": params["num"]},
            5.0,
        )

    if action == "create_subpatcher":
        required = {"position", "varname"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: create_subpatcher missing fields: {missing}",
                recoverable=False,
            )
        if _is_protected_varname(str(params["varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{params['varname']}' cannot be mutated.",
                recoverable=False,
            )
        return (
            {
                "action": "create_subpatcher",
                "position": params["position"],
                "varname": params["varname"],
                "name": params.get("name", "subpatch"),
            },
            6.0,
        )

    if action == "enter_subpatcher":
        if "varname" not in params:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: enter_subpatcher requires 'varname'.",
                recoverable=False,
            )
        return ({"action": "enter_subpatcher", "varname": params["varname"]}, 4.0)

    if action == "exit_subpatcher":
        return ({"action": "exit_subpatcher"}, 4.0)

    if action == "add_subpatcher_io":
        required = {"position", "io_type", "varname"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: add_subpatcher_io missing fields: {missing}",
                recoverable=False,
            )
        if _is_protected_varname(str(params["varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{params['varname']}' cannot be mutated.",
                recoverable=False,
            )
        return (
            {
                "action": "add_subpatcher_io",
                "position": params["position"],
                "io_type": params["io_type"],
                "varname": params["varname"],
                "comment": params.get("comment", ""),
            },
            5.0,
        )

    if action == "recreate_with_args":
        required = {"varname", "new_args"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: recreate_with_args missing fields: {missing}",
                recoverable=False,
            )
        if _is_protected_varname(str(params["varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{params['varname']}' cannot be mutated.",
                recoverable=False,
            )
        return (
            {
                "action": "recreate_with_args",
                "varname": params["varname"],
                "new_args": params["new_args"],
            },
            8.0,
        )

    if action == "move_object":
        required = {"varname", "x", "y"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: move_object missing fields: {missing}",
                recoverable=False,
            )
        if _is_protected_varname(str(params["varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname '{params['varname']}' cannot be mutated.",
                recoverable=False,
            )
        return (
            {
                "action": "move_object",
                "varname": params["varname"],
                "x": params["x"],
                "y": params["y"],
            },
            5.0,
        )

    if action == "encapsulate":
        required = {"varnames", "subpatcher_name", "subpatcher_varname"}
        missing = [field for field in required if field not in params]
        if missing:
            raise MaxMCPError(
                ERROR_VALIDATION,
                f"Step {step_idx}: encapsulate missing fields: {missing}",
                recoverable=False,
            )
        protected = [vn for vn in params["varnames"] if _is_protected_varname(str(vn))]
        if protected or _is_protected_varname(str(params["subpatcher_varname"])):
            raise MaxMCPError(
                ERROR_PROTECTED_OBJECT,
                f"Step {step_idx}: protected varname involved in encapsulate.",
                recoverable=False,
            )
        return (
            {
                "action": "encapsulate",
                "varnames": params["varnames"],
                "subpatcher_name": params["subpatcher_name"],
                "subpatcher_varname": params["subpatcher_varname"],
            },
            12.0,
        )

    raise MaxMCPError(
        ERROR_UNKNOWN_ACTION,
        f"Step {step_idx}: unsupported transaction action '{action}'.",
        recoverable=False,
    )


@mcp.tool()
async def run_patch_transaction(
    ctx: Context,
    steps: list,
    dry_run_engine: str = "maxpy",
    rollback_on_error: bool = True,
    checkpoint_label: str = "",
    idempotency_seed: str = "",
) -> dict:
    """Execute a multi-step patch transaction with optional rollback on failure."""
    runtime = _get_runtime(ctx)
    if runtime is None:
        return {"success": False, "error": "Runtime manager is unavailable."}
    if runtime.active_target == "host":
        return runtime._host_mutation_error("run_patch_transaction")

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    if maxmsp is None:
        return {"success": False, "error": "Bridge connection is unavailable."}

    preflight = await dry_run_plan(
        ctx,
        steps,
        engine=dry_run_engine,
        unknown_action_policy="error",
    )
    if not preflight.get("valid"):
        return {
            "success": False,
            "error": "Preflight dry-run failed. Transaction aborted.",
            "preflight": preflight,
        }

    checkpoint = await runtime.create_checkpoint(label=checkpoint_label or "transaction")
    if not checkpoint.get("success"):
        return {
            "success": False,
            "error": "Failed to create pre-transaction checkpoint.",
            "checkpoint": checkpoint,
        }

    tx_id = uuid.uuid4().hex[:10]
    step_results = []
    for idx, raw_step in enumerate(steps):
        step_index = idx + 1
        action = raw_step.get("action") if isinstance(raw_step, dict) else None
        params = raw_step.get("params", {}) if isinstance(raw_step, dict) else {}
        try:
            if not isinstance(action, str) or not isinstance(params, dict):
                raise MaxMCPError(
                    ERROR_VALIDATION,
                    f"Step {step_index}: each step must include action + params object.",
                    recoverable=False,
                )
            payload, timeout = _build_transaction_bridge_request(step_index, action, params)
            idem_key = (idempotency_seed.strip() or tx_id) + f":{step_index}"
            response = await maxmsp.send_request(
                payload,
                timeout=timeout,
                idempotency_key=idem_key,
            )
            if isinstance(response, dict) and response.get("success") is False:
                err = response.get("error", {})
                raise MaxMCPError(
                    err.get("code", ERROR_INTERNAL),
                    err.get("message", f"Step {step_index} failed."),
                    hint=err.get("hint"),
                    recoverable=bool(err.get("recoverable", True)),
                    details=err.get("details") if isinstance(err.get("details"), dict) else {},
                )
            step_results.append(
                {
                    "step": step_index,
                    "action": action,
                    "payload_action": payload.get("action"),
                    "success": True,
                    "result": response,
                }
            )
        except Exception as e:
            error_payload = (
                e.to_dict() if isinstance(e, MaxMCPError) else {"code": ERROR_INTERNAL, "message": str(e)}
            )
            rollback_result = None
            if rollback_on_error:
                rollback_result = await runtime.restore_checkpoint(checkpoint["checkpoint_id"])
            return {
                "success": False,
                "transaction_id": tx_id,
                "failed_step": step_index,
                "error": error_payload,
                "steps_completed": len(step_results),
                "step_results": step_results,
                "checkpoint": checkpoint,
                "rollback": rollback_result,
            }

    drift = await runtime.check_patch_drift(auto_resync=False)
    return {
        "success": True,
        "transaction_id": tx_id,
        "steps_completed": len(step_results),
        "step_results": step_results,
        "checkpoint": checkpoint,
        "post_transaction_drift": drift,
    }


# Math objects that require float arguments (or explicit int_mode)
# These objects default to integer mode which truncates floats - a common source of bugs
FLOAT_REQUIRED_OBJECTS = {"+", "-", "*", "/", "!+", "!-", "!*", "!/", "%", "pow", "scale"}

# Pack/unpack objects - require float arguments (or explicit int_mode) like math objects
# This prevents the common bug of [pack 0 100] outputting ints when used with line~
# and [unpack 0 0 0] truncating incoming floats to ints
PACK_OBJECTS = {"pack", "pak", "unpack"}

# Objects that should be rejected with a suggestion for the correct alternative
REJECTED_OBJECTS = {
    "times~": "*~",
}

# Objects with minimum argument requirements
MIN_ARGS_OBJECTS = {
    "comb~": {
        "min_args": 5,
        "usage": "[comb~ maxdelay delay feedback feedforward gain] e.g. [comb~ 1000 100 0.9 0.5 1.]",
    },
}

# Parameter range validations (require extend=True to bypass)
PARAM_RANGE_CHECKS = {
    "svf~": {
        "arg_index": 1,  # Q is second argument (after frequency)
        "check": lambda v: v >= 1,
        "error": "svf~ Q/resonance should be 0-1, not 0-100. Got {value}. "
                 "Set extend=True if you really want Q >= 1.",
    },
    "onepole~": {
        "arg_index": 0,  # frequency is first argument
        "check": lambda v: v < 10,
        "error": "onepole~ takes frequency in Hz (e.g., 5000), not a coefficient. Got {value}. "
                 "Set extend=True if you really want frequency < 10 Hz.",
    },
}


def _has_float_arg(args: list) -> bool:
    """Check if any argument is a float (not an integer).

    Also checks string args - if a string contains '.', it indicates float intent.
    This allows the model to pass ["0", "127", "0", "25."] to preserve float notation
    that would otherwise be lost during JSON serialization.
    """
    for arg in args:
        if isinstance(arg, float):
            return True
        # String with '.' indicates float intent (survives JSON)
        if isinstance(arg, str) and '.' in arg:
            try:
                float(arg)  # Verify it's a valid number
                return True
            except ValueError:
                pass
    return False


def _pack_has_float_arg(args: list) -> bool:
    """Check if pack/pak has at least one float argument or 'f' type specifier."""
    for arg in args:
        if isinstance(arg, float):
            return True
        if isinstance(arg, str) and arg.lower() == "f":
            return True
        # String with '.' indicates float intent
        if isinstance(arg, str) and '.' in arg:
            try:
                float(arg)
                return True
            except ValueError:
                pass
    return False


def _convert_string_args(args: list) -> list:
    """Convert string numeric args to proper types for Max.

    - Strings with '.' -> float (e.g., "25." -> 25.0)
    - Strings without '.' -> int (e.g., "127" -> 127)
    - Non-numeric strings pass through unchanged (e.g., "f", "@embed")
    - Already numeric types pass through unchanged
    """
    result = []
    for arg in args:
        if isinstance(arg, str):
            # Check if it's a numeric string
            if '.' in arg:
                try:
                    result.append(float(arg))
                    continue
                except ValueError:
                    pass
            else:
                try:
                    result.append(int(arg))
                    continue
                except ValueError:
                    pass
            # Not a number, keep as string
            result.append(arg)
        else:
            result.append(arg)
    return result


def _validate_add_max_object_payload(
    *,
    obj_type: str,
    args: list,
    int_mode: bool,
    extend: bool,
    use_live_dial: bool,
    trigger_rtl: bool,
) -> dict | None:
    # Reject objects with known alternatives
    if obj_type in REJECTED_OBJECTS:
        correct = REJECTED_OBJECTS[obj_type]
        return _error_result(
            ERROR_VALIDATION,
            f"WRONG OBJECT: '{obj_type}' does not exist. Use '{correct}' instead.",
        )

    # Validate minimum argument requirements
    if obj_type in MIN_ARGS_OBJECTS:
        req = MIN_ARGS_OBJECTS[obj_type]
        if len(args) < req["min_args"]:
            return _error_result(
                ERROR_VALIDATION,
                f"MISSING ARGUMENTS: '{obj_type}' requires at least {req['min_args']} arguments. Usage: {req['usage']}",
            )

    # Validate float requirement for math objects
    if obj_type in FLOAT_REQUIRED_OBJECTS:
        # Special case for scale: if output range is 0-1 or small, assume float intent
        scale_float_intent = False
        if obj_type == "scale" and len(args) >= 4:
            out_min, out_max = args[2], args[3]
            if isinstance(out_min, (int, float)) and isinstance(out_max, (int, float)):
                out_range = abs(out_max - out_min)
                # If output range is <= 2 (like 0-1, -1 to 1, 0-2), assume float intent
                if out_range <= 2:
                    scale_float_intent = True

        if not _has_float_arg(args) and not int_mode and not scale_float_intent:
            return _error_result(
                ERROR_VALIDATION,
                f"FLOAT REQUIRED: '{obj_type}' defaults to integer mode which truncates floats. "
                f"Use STRING args with '.' to preserve float type (JSON strips .0 from numbers). "
                f"Example: args: [\"0\", \"127\", \"0\", \"25.\"] instead of [0, 127, 0, 25.0]. "
                f"Or set int_mode=True if integer truncation is intended.",
            )

    # Validate float requirement for pack/pak/unpack objects
    if obj_type in PACK_OBJECTS:
        if not _pack_has_float_arg(args) and not int_mode:
            return _error_result(
                ERROR_VALIDATION,
                f"FLOAT REQUIRED: '{obj_type}' with integer arguments outputs integers. "
                f"Use 'f' type specifier: ['f', 'f', 'f'], or STRING args with '.': [\"0.\", \"0.\"], "
                f"or set int_mode=True if integer output is intended.",
            )

    # Validate parameter ranges (unless extend=True)
    if obj_type in PARAM_RANGE_CHECKS and not extend:
        check = PARAM_RANGE_CHECKS[obj_type]
        idx = check["arg_index"]
        if len(args) > idx:
            value = args[idx]
            if isinstance(value, (int, float)) and check["check"](value):
                return _error_result(
                    ERROR_VALIDATION,
                    f"PARAM RANGE: {check['error'].format(value=value)}",
                )

    # Reject live.dial by default - suggest dial instead
    if obj_type == "live.dial" and not use_live_dial:
        return _error_result(
            ERROR_VALIDATION,
            "USE DIAL INSTEAD: live.dial outputs 0-127 with no inline range control. "
            "Use [dial] with attributes instead:\n"
            "  - Float 0-1: [dial @size 1 @floatoutput 1]\n"
            "  - Float -1 to 1 (pan): [dial @min -1 @size 2 @floatoutput 1 @mode 6]\n"
            "  - Int 0-127: [dial @size 127]\n"
            "Set use_live_dial=True only if you specifically need Live integration.",
        )

    # Validate dial has explicit range attributes
    if obj_type == "dial":
        has_size = "@size" in args
        if not has_size:
            return _error_result(
                ERROR_VALIDATION,
                "RANGE REQUIRED: dial needs explicit @size attribute. Examples:\n"
                "  - Float 0-1: ['@size', 1, '@floatoutput', 1]\n"
                "  - Float -1 to 1 (pan): ['@min', -1, '@size', 2, '@floatoutput', 1, '@mode', 6]\n"
                "  - Int 0-127: ['@size', 127]",
            )

        # Check for excessively large dial sizes (makes UI unusable)
        if not extend:
            try:
                size_idx = args.index("@size")
                if size_idx + 1 < len(args):
                    size_val = args[size_idx + 1]
                    if isinstance(size_val, (int, float)) and size_val > 255:
                        return _error_result(
                            ERROR_VALIDATION,
                            f"DIAL SIZE TOO LARGE: @size {int(size_val)} creates unusable UI "
                            f"(must drag through {int(size_val)} positions). "
                            "For large ranges, use:\n"
                            "  - [flonum] or [number] for direct value entry\n"
                            "  - A scaled dial (e.g., 0-100 dial with multiplier)\n"
                            "Set extend=True to bypass this check.",
                        )
            except (ValueError, IndexError):
                pass  # @size not found or malformed - other validation handles this

    # Validate trigger/t right-to-left acknowledgment
    if obj_type in {"trigger", "t"} and not trigger_rtl:
        return _error_result(
            ERROR_VALIDATION,
            "ORDER ACKNOWLEDGMENT REQUIRED: trigger/t fires outlets RIGHT-TO-LEFT. "
            "The rightmost argument fires FIRST. For example, [t b f] sends 'f' first, then 'b'. "
            "Set trigger_rtl=True to acknowledge you understand this.",
        )

    # Validate coll has @embed 1 for data persistence
    if obj_type == "coll":
        has_embed = False
        for i, arg in enumerate(args):
            if arg == "@embed" and i + 1 < len(args) and args[i + 1] == 1:
                has_embed = True
                break
        if not has_embed:
            return _error_result(
                ERROR_VALIDATION,
                "EMBED REQUIRED: coll data does not persist on save unless @embed 1 is set. "
                "Use args like: ['mycoll', '@embed', 1] to ensure data is saved with the patch.",
            )

    return None


@mcp.tool()
async def add_max_object(
    ctx: Context,
    position: list,
    obj_type: str,
    varname: str,
    args: list,
    int_mode: bool = False,
    extend: bool = False,
    use_live_dial: bool = False,
    trigger_rtl: bool = False,
    idempotency_key: str = "",
):
    """Add a new Max object.

    The position is is a list of two integers representing the x and y coordinates,
    which should be outside the rectangular area returned by get_avoid_rect_position() function.

    Args:
        position (list): Position in the Max patch as [x, y].
        obj_type (str): Type of the Max object (e.g., "cycle~", "dac~").
        varname (str): Variable name for the object.
        args (list): Arguments for the object.
        int_mode (bool): For math objects (+, -, *, /, %, scale, etc.) and pack/pak,
                         set True to allow integer-only arguments. By default, these objects
                         require at least one float argument (or 'f' type specifier for pack/pak)
                         to prevent unintended integer truncation.
        extend (bool): Bypass parameter range checks. Use when you intentionally want
                       unusual values like svf~ Q >= 1 or onepole~ frequency < 10 Hz.
        use_live_dial (bool): Bypass the live.dial rejection. By default, use `dial` instead
                              which supports inline range attributes (@size, @min, @floatoutput, @mode).
        trigger_rtl (bool): Acknowledge that trigger/t objects fire outlets RIGHT-TO-LEFT.
                            The rightmost outlet fires first. Order your arguments accordingly.

    Returns:
        dict: Result with success/error status.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    validation_error = _validate_add_max_object_payload(
        obj_type=obj_type,
        args=args,
        int_mode=int_mode,
        extend=extend,
        use_live_dial=use_live_dial,
        trigger_rtl=trigger_rtl,
    )
    if validation_error:
        return validation_error

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    if not isinstance(position, list) or len(position) != 2:
        return _error_result(
            ERROR_VALIDATION,
            "Position must be a list of two integers.",
        )

    # Convert string args to proper types (preserves float intent from "25." strings)
    converted_args = _convert_string_args(args)

    payload = {
        "action": "add_object",
        "position": position,
        "obj_type": obj_type,
        "args": converted_args,
        "varname": varname,
    }
    response = await maxmsp.send_request(
        payload,
        timeout=5.0,
        idempotency_key=idempotency_key or None,
    )
    return response


@mcp.tool()
async def remove_max_object(
    ctx: Context,
    varname: str,
    idempotency_key: str = "",
):
    """Delete a Max object.

    Args:
        varname (str): Variable name for the object.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "remove_object", "varname": varname}
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def connect_max_objects(
    ctx: Context,
    src_varname: str,
    outlet_idx: int,
    dst_varname: str,
    inlet_idx: int,
    idempotency_key: str = "",
):
    """Connect two Max objects.

    Args:
        src_varname (str): Variable name of the source object.
        outlet_idx (int): Outlet index on the source object.
        dst_varname (str): Variable name of the destination object.
        inlet_idx (int): Inlet index on the destination object.
    """
    if _is_protected_varname(src_varname):
        return _protected_varname_error(src_varname)
    if _is_protected_varname(dst_varname):
        return _protected_varname_error(dst_varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {
        "action": "connect_objects",
        "src_varname": src_varname,
        "outlet_idx": outlet_idx,
        "dst_varname": dst_varname,
        "inlet_idx": inlet_idx,
    }
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def disconnect_max_objects(
    ctx: Context,
    src_varname: str,
    outlet_idx: int,
    dst_varname: str,
    inlet_idx: int,
    idempotency_key: str = "",
):
    """Disconnect two Max objects.

    Args:
        src_varname (str): Variable name of the source object.
        outlet_idx (int): Outlet index on the source object.
        dst_varname (str): Variable name of the destination object.
        inlet_idx (int): Inlet index on the destination object.
    """
    if _is_protected_varname(src_varname):
        return _protected_varname_error(src_varname)
    if _is_protected_varname(dst_varname):
        return _protected_varname_error(dst_varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {
        "action": "disconnect_objects",
        "src_varname": src_varname,
        "outlet_idx": outlet_idx,
        "dst_varname": dst_varname,
        "inlet_idx": inlet_idx,
    }
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def set_object_attribute(
    ctx: Context,
    varname: str,
    attr_name: str,
    attr_value: list,
    idempotency_key: str = "",
):
    """Set an attribute of a Max object.

    Args:
        varname (str): Variable name of the object.
        attr_name (str): Name of the attribute to be set.
        attr_value (list): Values of the attribute to be set.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {
        "action": "set_object_attribute",
        "varname": varname,
        "attr_name": attr_name,
        "attr_value": attr_value,
    }
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def set_message_text(
    ctx: Context,
    varname: str,
    text_list: list,
    not_line_msg: bool = False,
    idempotency_key: str = "",
):
    """Set the text of a message object in MaxMSP.

    Args:
        varname (str): Variable name of the message object.
        text_list (list): A list of arguments to be set to the message object.
        not_line_msg (bool): Set True if this message is NOT for line~/line.
                             By default, messages with 3+ numbers and an odd count
                             are rejected (likely malformed line~ target-time pairs).
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    # Check for likely malformed line~ messages (odd number of numeric values >= 3)
    if not not_line_msg:
        numeric_count = sum(1 for item in text_list if isinstance(item, (int, float)))
        if numeric_count >= 3 and numeric_count % 2 == 1:
            return _error_result(
                ERROR_VALIDATION,
                f"LIKELY MALFORMED LINE~ MESSAGE: Got {numeric_count} numeric values (odd count). "
                "line~/line expects target-time PAIRS. Examples:\n"
                "  - Instant to 0, ramp to 1 in 500ms, back to 0 in 500ms: [0, 0, 1, 500, 0, 500]\n"
                "  - Same with comma syntax: ['0,', 1, 500, 0, 500] (comma makes '0' instant)\n"
                "Set not_line_msg=True if this message is not for line~/line.",
            )

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "set_message_text", "varname": varname, "new_text": text_list}
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def send_bang_to_object(ctx: Context, varname: str, idempotency_key: str = ""):
    """Send a bang to an object in MaxMSP.

    Args:
        varname (str): Variable name of the object to be banged.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "send_bang_to_object", "varname": varname}
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def send_messages_to_object(
    ctx: Context,
    varname: str,
    message: list,
    idempotency_key: str = "",
):
    """Send a message to an object in MaxMSP. The message is made of a list of arguments.

    When using message to set attributes, one attribute can only be set by one message.
    For example, to set the "size" attribute of a "button" object, use:
    send_messages_to_object("button1", ["size", 100, 100])
    To set the "size" and "color" attributes of a "button" object, use the tool for two times:
    send_messages_to_object("button1", ["size", 100, 100])
    send_messages_to_object("button1", ["color", 0, 0, 0])

    Args:
        varname (str): Variable name of the object to be messaged.
        message (list): A list of messages to be sent to the object.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "send_message_to_object", "varname": varname, "message": message}
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def set_number(
    ctx: Context,
    varname: str,
    num: float,
    idempotency_key: str = "",
):
    """Set the value of a object in MaxMSP.
    The object can be a number box, a slider, a dial, a gain.

    Args:
        varname (str): Variable name of the comment object.
        num (float): Value to be set for the object.
    """

    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "set_number", "varname": varname, "num": num}
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
def list_all_objects(ctx: Context) -> list:
    """Returns a name list of all objects that can be added in Max.
    To understand a specific object in the list, use the `get_object_doc` tool."""
    return list(flattened_docs.keys())


@mcp.tool()
def search_objects(
    ctx: Context,
    query: str,
    package: str = "",
    limit: int = 20,
    include_aliases: bool = True,
) -> dict:
    """Search Max objects by name/alias using MaxPyLang metadata."""
    if not maxpy_catalog.available:
        return _error_result(
            ERROR_PRECONDITION,
            "MaxPyLang metadata index is unavailable.",
            hint=f"Expected metadata under {maxpy_catalog.obj_info_dir}",
            recoverable=True,
        )
    if not query.strip():
        return _error_result(
            ERROR_VALIDATION,
            "Query must be non-empty.",
            recoverable=True,
        )

    pkg = package.strip() or None
    rows = maxpy_catalog.search(
        query,
        package=pkg,
        limit=limit,
        include_aliases=include_aliases,
    )
    return {
        "query": query,
        "package": pkg,
        "count": len(rows),
        "results": rows,
        "schema_hash": maxpy_catalog.schema_hash,
    }


@mcp.tool()
def get_object_schema(ctx: Context, object_name: str, include_aliases: bool = True) -> dict:
    """Return MaxPyLang schema details for a single Max object."""
    if not maxpy_catalog.available:
        return _error_result(
            ERROR_PRECONDITION,
            "MaxPyLang metadata index is unavailable.",
            hint=f"Expected metadata under {maxpy_catalog.obj_info_dir}",
            recoverable=True,
        )

    schema = maxpy_catalog.get_schema(object_name)
    if schema is None:
        suggestions = maxpy_catalog.suggest(object_name)
        return _error_result(
            ERROR_OBJECT_NOT_FOUND,
            f"Object not found in MaxPyLang catalog: '{object_name}'.",
            hint="Use search_objects for discovery.",
            recoverable=True,
            details={"suggestions": suggestions},
        )

    if not include_aliases:
        schema["aliases"] = []

    doc = flattened_docs.get(schema["canonical_name"]) or flattened_docs.get(object_name)
    if doc:
        schema["doc"] = doc
    schema["schema_hash"] = maxpy_catalog.schema_hash
    return schema


@mcp.tool()
def get_object_doc(ctx: Context, object_name: str) -> dict:
    """Retrieve the official documentation for a given object.
    Use this resource to understand how a specific object works, including its
    description, inlets, outlets, arguments, methods(messages), and attributes.

    Args:
        object_name (str): Name of the object to look up.

    Returns:
        dict: Official documentations for the specified object.
    """
    try:
        return flattened_docs[object_name]
    except KeyError:
        suggestions = maxpy_catalog.suggest(object_name) if maxpy_catalog.available else []
        return _error_result(
            ERROR_VALIDATION,
            "Invalid object name.",
            hint="Make sure the object name is a valid Max object name.",
            details={"suggestions": suggestions},
        )


@mcp.tool()
async def get_objects_in_patch(
    ctx: Context,
):
    """Retrieve the list of existing objects in the current Max patch.

    Use this to understand the current state of the patch, including the
    objects(boxes) and patch cords(lines). The retrieved list contains a
    list of objects including their maxclass, varname for scripting,
    position(patching_rect), and the boxtext when available, as well as a
    list of patch cords with their source and destination information.

    Returns:
        list: A list of objects and patch cords.
    """
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "get_objects_in_patch"}
    response = await maxmsp.send_request(payload, timeout=5.0)

    return [response]


@mcp.tool()
async def get_objects_in_selected(
    ctx: Context,
):
    """Retrieve the list of objects that is selected in a (unlocked) patcher window.

    Use this when the user wanted to reference to the selected objects.

    Returns:
        list: A list of objects and patch cords.
    """
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "get_objects_in_selected"}
    response = await maxmsp.send_request(payload, timeout=5.0)

    return [response]


@mcp.tool()
async def get_object_attributes(ctx: Context, varname: str):
    """Retrieve an objects' attributes and values of the attributes.

    Use this to understand the state of an object.

    Returns:
        list: A list of attributes name and attributes values.
    """
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "get_object_attributes"}
    kwargs = {"varname": varname}
    payload.update(kwargs)
    response = await maxmsp.send_request(payload)

    return [response]


@mcp.tool()
async def get_avoid_rect_position(ctx: Context):
    """When deciding the position to add a new object to the path, this rectangular area
    should be avoid. This is useful when you want to add an object to the patch without
    overlapping with existing objects.

    Returns:
        list: A list of four numbers representing the left, top, right, bottom of the rectangular area.
    """
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "get_avoid_rect_position"}
    response = await maxmsp.send_request(payload)

    return response


@mcp.tool()
async def get_patch_context(
    ctx: Context,
    include_topology: bool = True,
    include_hierarchy: bool = True,
) -> dict:
    """Return a context-rich patch summary for planning and diagnostics."""
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    patch = await maxmsp.send_request({"action": "get_objects_in_patch"}, timeout=8.0)
    summary = {
        "object_count": 0,
        "connection_count": 0,
        "signal_object_count": 0,
        "control_object_count": 0,
        "maxclass_counts": {},
        "objects_without_varname": 0,
    }
    if isinstance(patch, dict):
        boxes = patch.get("boxes", [])
        lines = patch.get("lines", [])
        summary["object_count"] = len(boxes)
        summary["connection_count"] = len(lines)
        for item in boxes:
            box = item.get("box", {}) if isinstance(item, dict) else {}
            maxclass = box.get("maxclass", "unknown")
            summary["maxclass_counts"][maxclass] = summary["maxclass_counts"].get(maxclass, 0) + 1
            if isinstance(maxclass, str) and maxclass.endswith("~"):
                summary["signal_object_count"] += 1
            else:
                summary["control_object_count"] += 1
            if not box.get("varname"):
                summary["objects_without_varname"] += 1

    context = None
    if include_hierarchy:
        context = await maxmsp.send_request({"action": "get_patcher_context"}, timeout=2.0)

    response = {"summary": summary}
    if include_topology:
        response["topology"] = patch
    if include_hierarchy:
        response["hierarchy"] = context
    return response


# ========================================
# Subpatcher navigation tools:


@mcp.tool()
async def create_subpatcher(
    ctx: Context,
    position: list,
    varname: str,
    name: str = "subpatch",
    idempotency_key: str = "",
):
    """Create a new subpatcher (p object) in the current patcher context.

    After creating, use enter_subpatcher to navigate inside and add objects.
    The subpatcher will have no inlets/outlets initially - add them with add_subpatcher_io.

    Args:
        position (list): Position in the Max patch as [x, y].
        varname (str): Variable name for the subpatcher object (used to enter it later).
        name (str): Display name shown in the subpatcher title bar.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {
        "action": "create_subpatcher",
        "position": position,
        "varname": varname,
        "name": name,
    }
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def enter_subpatcher(ctx: Context, varname: str, idempotency_key: str = ""):
    """Navigate into a subpatcher to add/modify objects inside it.

    After entering, all object operations (add_max_object, connect_max_objects, etc.)
    will operate within this subpatcher context.

    Use exit_subpatcher to return to the parent patcher.

    Args:
        varname (str): Variable name of the subpatcher object to enter.
    """
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "enter_subpatcher", "varname": varname}
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def exit_subpatcher(ctx: Context, idempotency_key: str = ""):
    """Exit the current subpatcher and return to the parent patcher.

    If already at root level, this has no effect.
    """
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "exit_subpatcher"}
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def get_patcher_context(ctx: Context):
    """Get information about the current patcher navigation context.

    Returns the depth (0 = root), path of subpatcher names, and whether at root.

    Returns:
        dict: Context info with 'depth', 'path' (list of varnames), and 'is_root'.
    """
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "get_patcher_context"}
    response = await maxmsp.send_request(payload)
    return response


@mcp.tool()
async def add_subpatcher_io(
    ctx: Context,
    position: list,
    io_type: str,
    varname: str,
    comment: str = "",
    idempotency_key: str = "",
):
    """Add an inlet or outlet object inside a subpatcher.

    These create the connection points visible on the parent patcher's subpatcher object.
    Must be called while inside a subpatcher (after enter_subpatcher).

    Args:
        position (list): Position as [x, y]. Inlets should be at top, outlets at bottom.
        io_type (str): One of "inlet", "outlet", "inlet~", or "outlet~".
        varname (str): Variable name for the io object.
        comment (str): Optional assistance text shown when hovering over the inlet/outlet.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {
        "action": "add_subpatcher_io",
        "position": position,
        "io_type": io_type,
        "varname": varname,
        "comment": comment,
    }
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


# ========================================
# Object manipulation enhancements:


@mcp.tool()
async def get_object_connections(ctx: Context, varname: str):
    """Get all connections (inputs and outputs) for a specific object.

    Returns connection information that can be used to restore connections
    after recreating an object with different arguments.

    Args:
        varname (str): Variable name of the object.

    Returns:
        dict: Contains 'inputs' (connections coming INTO this object) and
              'outputs' (connections going OUT of this object).
    """
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "get_object_connections", "varname": varname}
    response = await maxmsp.send_request(payload)
    return response


@mcp.tool()
async def recreate_with_args(
    ctx: Context,
    varname: str,
    new_args: list,
    idempotency_key: str = "",
):
    """Recreate an existing object with new arguments, preserving all connections.

    This is an atomic operation that:
    1. Gets the object's current position, type, and connections
    2. Removes the object
    3. Creates a new object with the same type but new arguments
    4. Restores all input and output connections

    Useful for changing object parameters that can only be set at creation time.

    Args:
        varname (str): Variable name of the object to recreate.
        new_args (list): New arguments for the object.

    Returns:
        dict: Status of the operation including any errors.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "recreate_with_args", "varname": varname, "new_args": new_args}
    response = await maxmsp.send_request(
        payload,
        timeout=5.0,
        idempotency_key=idempotency_key or None,
    )
    return response


@mcp.tool()
async def move_object(
    ctx: Context,
    varname: str,
    x: int,
    y: int,
    idempotency_key: str = "",
):
    """Move an object to a new position in the patch.

    Args:
        varname (str): Variable name of the object to move.
        x (int): New x coordinate (pixels from left).
        y (int): New y coordinate (pixels from top).

    Returns:
        dict: Status of the operation.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "move_object", "varname": varname, "x": x, "y": y}
    response = await maxmsp.send_request(
        payload,
        idempotency_key=idempotency_key or None,
    )
    return response


@mcp.tool()
async def autofit_existing(
    ctx: Context,
    varname: str,
    idempotency_key: str = "",
):
    """Apply auto-fit sizing to an existing object.

    Resizes the object width to fit its text content.
    Skips UI objects like toggle, button, slider, etc.

    Args:
        varname (str): Variable name of the object to resize.
    """
    if _is_protected_varname(varname):
        return _protected_varname_error(varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "autofit_existing", "varname": varname}
    return await maxmsp.send_command(
        payload,
        idempotency_key=idempotency_key or None,
    )


@mcp.tool()
async def check_signal_safety(ctx: Context):
    """Analyze the current patch for potentially dangerous signal patterns.

    Checks for:
    - Dangerous feedback loops (excludes valid tapout~ -> tapin~ patterns)
    - High gain *~ objects (> 1.0)
    - Unsafe comb~ feedback values (>= 1.0)
    - Missing limiter (clip~, tanh~, etc.) before dac~

    Returns:
        dict: Contains 'warnings' list and 'safe' boolean.
    """
    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {"action": "check_signal_safety"}
    response = await maxmsp.send_request(payload, timeout=5.0)
    return response


@mcp.tool()
async def encapsulate(
    ctx: Context,
    varnames: list,
    subpatcher_name: str,
    subpatcher_varname: str,
    idempotency_key: str = "",
):
    """Encapsulate a set of objects into a new subpatcher.

    This is similar to Max's Edit > Encapsulate command. It takes the specified
    objects, moves them into a new subpatcher, and automatically creates inlets
    and outlets to preserve all external connections.

    Args:
        varnames (list): List of varnames of objects to encapsulate.
        subpatcher_name (str): Display name for the subpatcher (shown in title bar).
        subpatcher_varname (str): Variable name for the subpatcher object.

    Returns:
        dict: Status including number of objects encapsulated, inlets/outlets created.
    """
    protected = [vn for vn in varnames if _is_protected_varname(vn)]
    if protected:
        return _protected_varname_error(protected[0])
    if _is_protected_varname(subpatcher_varname):
        return _protected_varname_error(subpatcher_varname)

    maxmsp = ctx.request_context.lifespan_context.get("maxmsp")
    payload = {
        "action": "encapsulate",
        "varnames": varnames,
        "subpatcher_name": subpatcher_name,
        "subpatcher_varname": subpatcher_varname,
    }
    response = await maxmsp.send_request(
        payload,
        timeout=10.0,
        idempotency_key=idempotency_key or None,
    )
    return response


if __name__ == "__main__":
    mcp.run()
