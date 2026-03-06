from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from .protocol import DEFAULT_BRIDGE_PROTO


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_auth_token_from_sources(
    env_token: str | None,
    token_file: Path,
) -> tuple[str, str]:
    token = (env_token or "").strip()
    if token:
        return token, "env"
    try:
        token_from_file = token_file.read_text().strip()
    except Exception:
        token_from_file = ""
    if token_from_file:
        return token_from_file, "file"
    return "", "unset"


def parse_path_roots(raw: str, base_dir: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    base = Path(base_dir or Path.cwd()).resolve()
    for part in str(raw or "").split(os.pathsep):
        value = part.strip()
        if not value:
            continue
        candidate = Path(os.path.expanduser(value))
        candidate = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        roots.append(candidate)
    return roots


@dataclass(frozen=True)
class NetworkSettings:
    socketio_server_url: str
    socketio_server_port: str
    namespace: str


@dataclass(frozen=True)
class PathSettings:
    root_dir: Path
    max_app_path: Path
    host_patch_path: Path
    fallback_patch_path: Path
    state_dir: Path
    state_file: Path
    npm_project_dir: Path
    npm_sentinel: Path
    sessions_root: Path
    session_id: str
    maxpy_root: Path
    maxpy_template_path: Path
    maxdevtools_root: Path
    qa_reports_dir: Path
    server_lock_path: Path
    auth_token_file: Path
    protected_varname_prefix: str


@dataclass(frozen=True)
class RuntimeSettings:
    protocol_version: str
    bridge_proto: str
    startup_mode: str
    multi_client_mode: str
    shared_daemon_host: str
    shared_daemon_port: int
    shared_daemon_start_timeout_seconds: float
    heartbeat_interval_seconds: float
    stale_threshold_seconds: float
    idempotency_cache_size: int
    checkpoint_max: int
    managed_mode: bool
    npm_auto_install: bool
    twin_auto_sync: bool
    strict_v3: bool
    strict_capability_gating: bool
    require_healthy_ready: bool
    require_handshake_auth: bool
    mutation_max_inflight: int
    mutation_max_queue: int
    mutation_queue_wait_timeout_seconds: float
    enforce_patch_roots: bool
    allowed_patch_roots_raw: str
    preflight_mode: str
    preflight_cache_seconds: float
    workspace_capture_timeout_seconds: float
    workspace_capture_retries: int
    workspace_capture_backoff_seconds: float
    health_check_cooldown_seconds: float
    failure_backoff_max_seconds: float
    transport_failure_clear_caps_threshold: int
    import_apply_timeout_seconds: float
    import_apply_retry_count: int
    import_apply_retry_backoff_seconds: float
    import_apply_chunk_size: int
    maxpylang_check_extended_timeout_seconds: float
    maxpylang_check_extended_cmd: str
    server_lock_wait_seconds: float
    server_lock_retry_interval_seconds: float
    server_lock_takeover_mode: str
    server_lock_takeover_grace_seconds: float
    auth_token: str
    auth_token_source: str


@dataclass(frozen=True)
class MetricsSettings:
    sample_size: int
    event_log_size: int
    log_interval_seconds: float
    alert_failure_rate: float
    alert_p95_ms: float
    alert_queue_depth: float
    alert_window_seconds: float


@dataclass(frozen=True)
class HygieneSettings:
    auto_cleanup: bool
    scope: str
    mode: str
    stale_seconds: int
    startup_sweep: bool
    report_max: int
    max_kills_per_sweep: int
    enable_window_scan: bool
    loop_interval_seconds: float
    keep_recent_sessions: int


@dataclass(frozen=True)
class Settings:
    network: NetworkSettings
    paths: PathSettings
    runtime: RuntimeSettings
    metrics: MetricsSettings
    hygiene: HygieneSettings

    _ALIASES = {
        "current_dir": ("paths", "root_dir"),
        "maxpylang_root": ("paths", "maxpy_root"),
        "maxpylang_template_path": ("paths", "maxpy_template_path"),
        "metrics_sample_size": ("metrics", "sample_size"),
        "metrics_log_interval_seconds": ("metrics", "log_interval_seconds"),
        "hygiene_auto_cleanup": ("hygiene", "auto_cleanup"),
        "hygiene_scope": ("hygiene", "scope"),
        "hygiene_mode": ("hygiene", "mode"),
        "hygiene_stale_seconds": ("hygiene", "stale_seconds"),
        "hygiene_startup_sweep": ("hygiene", "startup_sweep"),
        "hygiene_report_max": ("hygiene", "report_max"),
        "hygiene_max_kills_per_sweep": ("hygiene", "max_kills_per_sweep"),
        "hygiene_enable_window_scan": ("hygiene", "enable_window_scan"),
        "hygiene_loop_interval_seconds": ("hygiene", "loop_interval_seconds"),
        "hygiene_keep_recent_sessions": ("hygiene", "keep_recent_sessions"),
    }

    @property
    def allowed_patch_roots(self) -> list[Path]:
        return parse_path_roots(self.runtime.allowed_patch_roots_raw, self.paths.root_dir)

    def __getattr__(self, name: str):
        alias = self._ALIASES.get(name)
        if alias is not None:
            section_name, attr_name = alias
            return getattr(getattr(self, section_name), attr_name)
        for section_name in ("network", "paths", "runtime", "metrics", "hygiene"):
            section = getattr(self, section_name)
            if hasattr(section, name):
                return getattr(section, name)
        raise AttributeError(name)


def load_settings(root_dir: Path | None = None) -> Settings:
    repo_root = Path(root_dir or Path(__file__).resolve().parents[1]).resolve()

    network = NetworkSettings(
        socketio_server_url=os.environ.get("SOCKETIO_SERVER_URL", "http://127.0.0.1"),
        socketio_server_port=os.environ.get("SOCKETIO_SERVER_PORT", "5002"),
        namespace=os.environ.get("NAMESPACE", "/mcp"),
    )

    state_dir = Path(os.path.expanduser(os.environ.get("MAXMCP_STATE_DIR", "~/.maxmsp-mcp")))
    npm_project_dir = repo_root / "MaxMSP_Agent"
    auth_token_file = Path(
        os.path.expanduser(os.environ.get("MAXMCP_AUTH_TOKEN_FILE", "~/.maxmsp-mcp/auth_token"))
    )
    maxpy_root = Path(
        os.environ.get(
            "MAXMCP_MAXPY_ROOT",
            str(repo_root / "refs" / "MaxPyLang-main" / "maxpylang"),
        )
    )

    paths = PathSettings(
        root_dir=repo_root,
        max_app_path=Path(os.environ.get("MAXMCP_MAX_APP", "/Applications/Max.app")),
        host_patch_path=Path(
            os.environ.get("MAXMCP_HOST_PATCH", str(repo_root / "MaxMSP_Agent" / "mcp_host.maxpat"))
        ),
        fallback_patch_path=repo_root / "MaxMSP_Agent" / "demo.maxpat",
        state_dir=state_dir,
        state_file=state_dir / "state.json",
        npm_project_dir=npm_project_dir,
        npm_sentinel=npm_project_dir / "node_modules" / "socket.io",
        sessions_root=Path(
            os.environ.get("MAXMCP_SESSIONS_ROOT", str(repo_root / "target" / "maxmcp" / "sessions"))
        ),
        session_id=os.environ.get("MAXMCP_SESSION_ID", uuid.uuid4().hex[:12]),
        maxpy_root=maxpy_root,
        maxpy_template_path=maxpy_root / "data" / "PATCH_TEMPLATES" / "empty_template.json",
        maxdevtools_root=Path(
            os.path.expanduser(
                os.environ.get("MAXMCP_MAXDEVTOOLS_ROOT", "/Users/gjb/Projects/_max/maxdevtools")
            )
        ),
        qa_reports_dir=Path(
            os.path.expanduser(os.environ.get("MAXMCP_QA_REPORTS_DIR", str(repo_root / "target" / "qa_reports")))
        ),
        server_lock_path=Path(
            os.path.expanduser(
                os.environ.get("MAXMCP_SERVER_LOCK_PATH", str(repo_root / "target" / "maxmcp" / "server.lock"))
            )
        ),
        auth_token_file=auth_token_file,
        protected_varname_prefix="__maxmcp_bridge_",
    )

    auth_token, auth_token_source = resolve_auth_token_from_sources(
        os.environ.get("MAXMCP_AUTH_TOKEN", ""),
        auth_token_file,
    )

    preflight_mode = os.environ.get("MAXMCP_PREFLIGHT_MODE", "auto").strip().lower()
    if preflight_mode not in {"auto", "manual", "session"}:
        logging.warning(
            "Invalid MAXMCP_PREFLIGHT_MODE '%s'; falling back to 'auto'.",
            preflight_mode,
        )
        preflight_mode = "auto"

    startup_mode = os.environ.get("MAXMCP_STARTUP_MODE", "fast_attach").strip().lower() or "fast_attach"
    if startup_mode not in {"fast_attach", "strict_ready"}:
        logging.warning(
            "Invalid MAXMCP_STARTUP_MODE '%s'; falling back to 'fast_attach'.",
            startup_mode,
        )
        startup_mode = "fast_attach"
    multi_client_mode = (
        os.environ.get("MAXMCP_MULTI_CLIENT_MODE", "shared_daemon").strip().lower()
        or "shared_daemon"
    )
    if multi_client_mode not in {"shared_daemon", "single"}:
        logging.warning(
            "Invalid MAXMCP_MULTI_CLIENT_MODE '%s'; falling back to 'shared_daemon'.",
            multi_client_mode,
        )
        multi_client_mode = "shared_daemon"
    shared_daemon_host = (
        os.environ.get("MAXMCP_SHARED_DAEMON_HOST", "127.0.0.1").strip() or "127.0.0.1"
    )
    try:
        shared_daemon_port = int(os.environ.get("MAXMCP_SHARED_DAEMON_PORT", "8765"))
    except Exception:
        shared_daemon_port = 8765
    shared_daemon_port = max(0, shared_daemon_port)
    shared_daemon_start_timeout_seconds = max(
        1.0,
        float(os.environ.get("MAXMCP_SHARED_DAEMON_START_TIMEOUT_SECONDS", "15")),
    )

    strict_v3 = env_bool("MAXMCP_STRICT_V3", True)
    if not strict_v3:
        logging.warning(
            "Protocol strict mode hard-cutover is active. "
            "MAXMCP_STRICT_V3 disabling is ignored."
        )

    takeover_mode = os.environ.get("MAXMCP_SERVER_LOCK_TAKEOVER_MODE", "safe").strip().lower() or "safe"
    if takeover_mode not in {"safe", "off"}:
        logging.warning(
            "Invalid MAXMCP_SERVER_LOCK_TAKEOVER_MODE '%s'; falling back to 'safe'.",
            takeover_mode,
        )
        takeover_mode = "safe"

    runtime = RuntimeSettings(
        protocol_version="2.0",
        bridge_proto=os.environ.get("MAXMCP_BRIDGE_PROTO", DEFAULT_BRIDGE_PROTO).strip() or DEFAULT_BRIDGE_PROTO,
        startup_mode=startup_mode,
        multi_client_mode=multi_client_mode,
        shared_daemon_host=shared_daemon_host,
        shared_daemon_port=shared_daemon_port,
        shared_daemon_start_timeout_seconds=shared_daemon_start_timeout_seconds,
        heartbeat_interval_seconds=float(os.environ.get("MAXMCP_HEARTBEAT_INTERVAL_SECONDS", "10")),
        stale_threshold_seconds=float(os.environ.get("MAXMCP_STALE_THRESHOLD_SECONDS", "30")),
        idempotency_cache_size=int(os.environ.get("MAXMCP_IDEMPOTENCY_CACHE_SIZE", "512")),
        checkpoint_max=int(os.environ.get("MAXMCP_CHECKPOINT_MAX", "20")),
        managed_mode=env_bool("MAXMCP_MANAGED_MODE", True),
        npm_auto_install=env_bool("MAXMCP_NPM_AUTO_INSTALL", True),
        twin_auto_sync=env_bool("MAXMCP_TWIN_AUTO_SYNC", True),
        strict_v3=strict_v3,
        strict_capability_gating=env_bool("MAXMCP_STRICT_CAPABILITY_GATING", True),
        require_healthy_ready=env_bool("MAXMCP_REQUIRE_HEALTHY_READY", True),
        require_handshake_auth=env_bool("MAXMCP_REQUIRE_HANDSHAKE_AUTH", True),
        mutation_max_inflight=int(os.environ.get("MAXMCP_MUTATION_MAX_INFLIGHT", "2")),
        mutation_max_queue=int(os.environ.get("MAXMCP_MUTATION_MAX_QUEUE", "32")),
        mutation_queue_wait_timeout_seconds=float(
            os.environ.get("MAXMCP_MUTATION_QUEUE_WAIT_TIMEOUT_SECONDS", "15")
        ),
        enforce_patch_roots=env_bool("MAXMCP_ENFORCE_PATCH_ROOTS", False),
        allowed_patch_roots_raw=os.environ.get("MAXMCP_ALLOWED_PATCH_ROOTS", "").strip(),
        preflight_mode=preflight_mode,
        preflight_cache_seconds=float(os.environ.get("MAXMCP_PREFLIGHT_CACHE_SECONDS", "30")),
        workspace_capture_timeout_seconds=float(
            os.environ.get("MAXMCP_WORKSPACE_CAPTURE_TIMEOUT_SECONDS", "8")
        ),
        workspace_capture_retries=int(os.environ.get("MAXMCP_WORKSPACE_CAPTURE_RETRIES", "2")),
        workspace_capture_backoff_seconds=float(
            os.environ.get("MAXMCP_WORKSPACE_CAPTURE_BACKOFF_SECONDS", "0.5")
        ),
        health_check_cooldown_seconds=float(
            os.environ.get("MAXMCP_HEALTH_CHECK_COOLDOWN_SECONDS", "2.0")
        ),
        failure_backoff_max_seconds=float(os.environ.get("MAXMCP_FAILURE_BACKOFF_MAX_SECONDS", "30.0")),
        transport_failure_clear_caps_threshold=int(
            os.environ.get("MAXMCP_TRANSPORT_FAILURE_CLEAR_CAPS_THRESHOLD", "2")
        ),
        import_apply_timeout_seconds=float(os.environ.get("MAXMCP_IMPORT_APPLY_TIMEOUT_SECONDS", "25")),
        import_apply_retry_count=int(os.environ.get("MAXMCP_IMPORT_APPLY_RETRY_COUNT", "1")),
        import_apply_retry_backoff_seconds=float(
            os.environ.get("MAXMCP_IMPORT_APPLY_RETRY_BACKOFF_SECONDS", "0.5")
        ),
        import_apply_chunk_size=int(os.environ.get("MAXMCP_IMPORT_APPLY_CHUNK_SIZE", "64")),
        maxpylang_check_extended_timeout_seconds=float(
            os.environ.get("MAXMCP_MAXPYLANG_CHECK_EXTENDED_TIMEOUT_SECONDS", "25")
        ),
        maxpylang_check_extended_cmd=os.environ.get("MAXMCP_MAXPYLANG_CHECK_EXTENDED_CMD", "").strip(),
        server_lock_wait_seconds=max(0.0, float(os.environ.get("MAXMCP_SERVER_LOCK_WAIT_SECONDS", "15"))),
        server_lock_retry_interval_seconds=max(
            0.001,
            float(os.environ.get("MAXMCP_SERVER_LOCK_RETRY_INTERVAL_SECONDS", "0.2")),
        ),
        server_lock_takeover_mode=takeover_mode,
        server_lock_takeover_grace_seconds=max(
            0.0,
            float(os.environ.get("MAXMCP_SERVER_LOCK_TAKEOVER_GRACE_SECONDS", "3.0")),
        ),
        auth_token=auth_token,
        auth_token_source=auth_token_source,
    )

    metrics = MetricsSettings(
        sample_size=int(os.environ.get("MAXMCP_METRICS_SAMPLE_SIZE", "512")),
        event_log_size=int(os.environ.get("MAXMCP_EVENT_LOG_SIZE", "256")),
        log_interval_seconds=float(os.environ.get("MAXMCP_METRICS_LOG_INTERVAL_SECONDS", "30")),
        alert_failure_rate=float(os.environ.get("MAXMCP_ALERT_FAILURE_RATE", "0.10")),
        alert_p95_ms=float(os.environ.get("MAXMCP_ALERT_P95_MS", "1500")),
        alert_queue_depth=float(os.environ.get("MAXMCP_ALERT_QUEUE_DEPTH", "0.80")),
        alert_window_seconds=float(os.environ.get("MAXMCP_ALERT_WINDOW_SECONDS", "300")),
    )

    hygiene = HygieneSettings(
        auto_cleanup=env_bool("MAXMCP_HYGIENE_AUTO_CLEANUP", True),
        scope=os.environ.get("MAXMCP_HYGIENE_SCOPE", "all_max_instances").strip(),
        mode=os.environ.get("MAXMCP_HYGIENE_MODE", "aggressive").strip(),
        stale_seconds=int(os.environ.get("MAXMCP_HYGIENE_STALE_SECONDS", "1800")),
        startup_sweep=env_bool("MAXMCP_HYGIENE_STARTUP_SWEEP", True),
        report_max=int(os.environ.get("MAXMCP_HYGIENE_REPORT_MAX", "500")),
        max_kills_per_sweep=int(os.environ.get("MAXMCP_HYGIENE_MAX_KILLS_PER_SWEEP", "50")),
        enable_window_scan=env_bool("MAXMCP_HYGIENE_ENABLE_WINDOW_SCAN", True),
        loop_interval_seconds=float(os.environ.get("MAXMCP_HYGIENE_LOOP_INTERVAL_SECONDS", "60")),
        keep_recent_sessions=int(os.environ.get("MAXMCP_HYGIENE_KEEP_RECENT_SESSIONS", "2")),
    )

    return Settings(
        network=network,
        paths=paths,
        runtime=runtime,
        metrics=metrics,
        hygiene=hygiene,
    )


SETTINGS = load_settings()
