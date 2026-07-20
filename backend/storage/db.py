from pathlib import Path
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
import uuid

import sqlite3

#找到当前文件（向上俩级目录）
BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = Path(os.getenv("SRE_AGENT_DB_PATH", BASE_DIR / "sre_agent.db"))
DB_PATH = DEFAULT_DB_PATH

MIGRATION_MANIFEST = (
    (1, "legacy_operational_schema", "2026-07-19-core-v1"),
    (2, "durable_changes_and_incidents", "2026-07-19-reliability-v2"),
    (3, "workspace_keys_and_usage", "2026-07-19-commercial-v3"),
    (4, "usage_cost_attribution", "2026-07-19-commercial-v4"),
    (5, "controlled_dead_letter_redrive", "2026-07-19-reliability-v5"),
    (6, "tamper_evident_audit_ledger", "2026-07-19-compliance-v6"),
    (7, "worker_heartbeat_readiness", "2026-07-19-reliability-v7"),
    (8, "pilot_value_outcomes", "2026-07-20-commercial-v8"),
    (9, "subscription_lifecycle", "2026-07-20-commercial-v9"),
)
CURRENT_SCHEMA_VERSION = MIGRATION_MANIFEST[-1][0]
_POSTGRES_MIGRATION_LOCK_ID = 7_361_904_211
AUDIT_LEDGER_LOCK_ID = 7_361_904_212


def is_postgres_database() -> bool:
    database_url = os.getenv("DATABASE_URL", "").strip().lower()
    return database_url.startswith("postgresql://") or database_url.startswith("postgres://")


def database_backend_name() -> str:
    return "postgresql" if is_postgres_database() else "sqlite"


def configured_workspace_id() -> str:
    value = os.getenv("SRE_WORKSPACE_ID", "default").strip()
    return value or "default"


def _replace_qmark_placeholders(sql: str) -> str:
    result = []
    in_single_quote = False
    in_double_quote = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'" and not in_double_quote:
            if in_single_quote and index + 1 < len(sql) and sql[index + 1] == "'":
                result.extend((char, char))
                index += 2
                continue
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        if char == "?" and not in_single_quote and not in_double_quote:
            result.append("%s")
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _postgres_sql(sql: str) -> str:
    stripped = sql.strip()
    if stripped.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    pragma_match = re.fullmatch(
        r"PRAGMA\s+table_info\(([^)]+)\)",
        stripped,
        flags=re.IGNORECASE,
    )
    if pragma_match:
        table_name = pragma_match.group(1).strip().strip("'\"")
        return (
            "SELECT column_name AS name FROM information_schema.columns "
            f"WHERE table_schema = current_schema() AND table_name = '{table_name}'"
        )
    converted = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )
    if re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", converted, re.IGNORECASE):
        converted = re.sub(
            r"INSERT\s+OR\s+IGNORE\s+INTO\s+",
            "INSERT INTO ",
            converted,
            count=1,
            flags=re.IGNORECASE,
        ).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return _replace_qmark_placeholders(converted)


class _PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql: str, params=None):
        self._cursor.execute(_postgres_sql(sql), params or ())
        return self

    def executemany(self, sql: str, params_seq):
        self._cursor.executemany(_postgres_sql(sql), params_seq)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return None


class _PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return _PostgresCursor(self._connection.cursor())

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def _get_postgres_conn():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "DATABASE_URL selects PostgreSQL but psycopg is not installed"
        ) from exc
    database_url = os.getenv("DATABASE_URL", "").strip()
    connection = psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10)
    return _PostgresConnection(connection)

#连接数据库
def get_conn():
    if is_postgres_database():
        return _get_postgres_conn()
    global DB_PATH
    DB_PATH = _resolve_db_path()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _resolve_db_path() -> Path:
    configured_path = os.getenv("SRE_AGENT_DB_PATH", "").strip()
    primary_path = Path(configured_path) if configured_path else BASE_DIR / "sre_agent.db"
    candidates = [
        primary_path,
        DEFAULT_DB_PATH,
        BASE_DIR / "sre_agent.db",
        Path("/tmp/sre_agent.db"),
    ]
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue
    raise OSError("unable to resolve writable sqlite database path")

def _migration_checksum(version: int, name: str, fingerprint: str) -> str:
    value = f"{version}:{name}:{fingerprint}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_audit_payload(values: dict) -> str:
    """Return the stable representation committed to the audit hash chain."""
    payload = {
        "event_id": values.get("event_id"),
        "action": values.get("action"),
        "service_name": values.get("service_name"),
        "source": values.get("source"),
        "status": values.get("status"),
        "reason": values.get("reason"),
        "actor": values.get("actor"),
        "change_request_id": values.get("change_request_id"),
        "created_at": values.get("created_at"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit_ledger_entry_hash(previous_hash: str, payload_json: str) -> str:
    return hashlib.sha256(f"{previous_hash}\n{payload_json}".encode("utf-8")).hexdigest()


def _initialize_schema(conn, cur):

    def _safe_add_column(table_name: str, column_name: str, column_sql: str):
        if is_postgres_database():
            cur.execute(
                f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_sql}"
            )
            return
        try:
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


#数据库初始化

    #服务监控表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS services (
        name TEXT PRIMARY KEY,
        version TEXT NOT NULL,
        status TEXT NOT NULL,
        cpu REAL NOT NULL,
        memory REAL NOT NULL,
        error_rate REAL NOT NULL,
        replicas INTEGER NOT NULL,
        last_deploy_time TEXT NOT NULL
    )
    """)
    #告警记录表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service TEXT NOT NULL,
        severity TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        resolved INTEGER NOT NULL DEFAULT 0
    )
    """)
    #日志存储表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        level TEXT NOT NULL,
        message TEXT NOT NULL
    )
    """)
    #部署历史表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS deployments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service TEXT NOT NULL,
        old_version TEXT NOT NULL,
        new_version TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # 任务执行记录表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_message TEXT NOT NULL,
        intent TEXT NOT NULL,
        final_answer TEXT NOT NULL,
        generation_source TEXT,
        llm_provider TEXT,
        used_fallback INTEGER,
        fallback_reason TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("PRAGMA table_info(task_runs)")
    existing_columns = {row["name"] for row in cur.fetchall()}
    if "generation_source" not in existing_columns:
        cur.execute("ALTER TABLE task_runs ADD COLUMN generation_source TEXT")
    if "llm_provider" not in existing_columns:
        cur.execute("ALTER TABLE task_runs ADD COLUMN llm_provider TEXT")
    if "used_fallback" not in existing_columns:
        cur.execute("ALTER TABLE task_runs ADD COLUMN used_fallback INTEGER")
    if "fallback_reason" not in existing_columns:
        cur.execute("ALTER TABLE task_runs ADD COLUMN fallback_reason TEXT")



    # 执行审计表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS execution_audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        service_name TEXT,
        source TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        actor TEXT,
        change_request_id TEXT,
        created_at TEXT NOT NULL
    )
    """)
    _safe_add_column("execution_audits", "actor", "TEXT")
    _safe_add_column("execution_audits", "change_request_id", "TEXT")
    _safe_add_column("execution_audits", "event_id", "TEXT")
    cur.execute("SELECT id FROM execution_audits WHERE event_id IS NULL ORDER BY id ASC")
    for row in cur.fetchall():
        cur.execute(
            "UPDATE execution_audits SET event_id = ? WHERE id = ?",
            (f"legacy:{row['id']}", row["id"]),
        )
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_audits_event_id
    ON execution_audits(event_id)
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_execution_audits_created
    ON execution_audits(created_at)
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_execution_audits_change_request
    ON execution_audits(change_request_id)
    """)

    # The ledger is the canonical, append-only audit view. Each entry commits to
    # the previous hash and a canonical payload so edits/reordering are detectable.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_ledger (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        action TEXT NOT NULL,
        service_name TEXT,
        source TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        actor TEXT,
        change_request_id TEXT,
        created_at TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        entry_hash TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_audit_ledger_created
    ON audit_ledger(created_at)
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_audit_ledger_change_request
    ON audit_ledger(change_request_id)
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_ledger_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pruned_through_sequence INTEGER NOT NULL,
        pruned_entry_count INTEGER NOT NULL,
        head_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        reason TEXT NOT NULL
    )
    """)
    cur.execute(
        "SELECT entry_hash FROM audit_ledger ORDER BY sequence DESC LIMIT 1"
    )
    ledger_head = cur.fetchone()
    if ledger_head:
        previous_hash = ledger_head["entry_hash"]
    else:
        cur.execute(
            "SELECT head_hash FROM audit_ledger_checkpoints ORDER BY id DESC LIMIT 1"
        )
        checkpoint = cur.fetchone()
        previous_hash = checkpoint["head_hash"] if checkpoint else ""
    cur.execute("""
        SELECT id, event_id, action, service_name, source, status, reason,
               actor, change_request_id, created_at
        FROM execution_audits
        WHERE event_id NOT IN (SELECT event_id FROM audit_ledger)
        ORDER BY id ASC
    """)
    for row in cur.fetchall():
        values = dict(row)
        payload_json = canonical_audit_payload(values)
        entry_hash = audit_ledger_entry_hash(previous_hash, payload_json)
        cur.execute(
            """
            INSERT INTO audit_ledger (
                event_id, action, service_name, source, status, reason, actor,
                change_request_id, created_at, previous_hash, entry_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["event_id"], values["action"], values["service_name"],
                values["source"], values["status"], values["reason"], values["actor"],
                values["change_request_id"], values["created_at"], previous_hash,
                entry_hash, payload_json,
            ),
        )
        previous_hash = entry_hash

    # 服务端持有的高风险变更请求。客户端只拿到不透明 ID，确认时重新读取并校验，
    # 避免客户端篡改服务名、目标版本或重复提交同一操作。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS change_requests (
        id TEXT PRIMARY KEY,
        action_type TEXT NOT NULL,
        service_name TEXT NOT NULL,
        target_version TEXT,
        policy_json TEXT NOT NULL,
        resolved_entities_json TEXT,
        status TEXT NOT NULL,
        requested_by TEXT,
        approved_by TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        confirmed_at TEXT,
        completed_at TEXT,
        result_json TEXT
    )
    """)
    _safe_add_column("change_requests", "requested_by", "TEXT")
    _safe_add_column("change_requests", "approved_by", "TEXT")
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_change_requests_status_expires
    ON change_requests(status, expires_at)
    """)

    # Durable jobs decouple approval requests from potentially long-running executors.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS change_jobs (
        id TEXT PRIMARY KEY,
        change_request_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        dry_run INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 1,
        available_at TEXT NOT NULL,
        locked_by TEXT,
        locked_at TEXT,
        last_error TEXT,
        redrive_count INTEGER NOT NULL DEFAULT 0,
        redriven_by TEXT,
        redriven_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_change_jobs_claim
    ON change_jobs(status, available_at, created_at)
    """)
    _safe_add_column("change_jobs", "redrive_count", "INTEGER NOT NULL DEFAULT 0")
    _safe_add_column("change_jobs", "redriven_by", "TEXT")
    _safe_add_column("change_jobs", "redriven_at", "TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS worker_heartbeats (
        worker_id TEXT PRIMARY KEY,
        hostname TEXT NOT NULL,
        process_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_worker_heartbeats_last_seen
    ON worker_heartbeats(last_seen_at)
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS incidents (
        id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        service_name TEXT,
        severity TEXT NOT NULL,
        status TEXT NOT NULL,
        owner TEXT,
        summary TEXT,
        alert_count INTEGER NOT NULL DEFAULT 0,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        resolved_at TEXT
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_incidents_status_updated
    ON incidents(status, updated_at)
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS incident_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT NOT NULL,
        alert_source TEXT NOT NULL,
        alert_ref TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(alert_source, alert_ref)
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_incident_alerts_incident
    ON incident_alerts(incident_id, created_at)
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS incident_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_incident_events_incident
    ON incident_events(incident_id, created_at)
    """)
    # 任务步骤记录表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_run_id INTEGER NOT NULL,
        step_no INTEGER NOT NULL,
        action TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # 运行时配置表（用于前端可编辑配置）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL
    )
    """)

    # 用户直接填写的外部服务目标（无额外 API 时使用主动探测）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS monitored_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        base_url TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # 多轮对话上下文，用于记住最近一次解析出的服务和动作
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_sessions (
        session_id TEXT PRIMARY KEY,
        last_service_name TEXT,
        last_intent TEXT,
        last_version TEXT,
        last_env TEXT,
        last_namespace TEXT,
        last_cluster TEXT,
        last_region TEXT,
        last_action_target TEXT,
        last_time_window_minutes INTEGER,
        pending_intent TEXT,
        pending_missing_fields TEXT,
        pending_question TEXT,
        pending_options TEXT,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("PRAGMA table_info(chat_sessions)")
    session_columns = {row["name"] for row in cur.fetchall()}
    if "last_namespace" not in session_columns:
        _safe_add_column("chat_sessions", "last_namespace", "TEXT")
    if "last_cluster" not in session_columns:
        _safe_add_column("chat_sessions", "last_cluster", "TEXT")
    if "last_region" not in session_columns:
        _safe_add_column("chat_sessions", "last_region", "TEXT")
    if "last_action_target" not in session_columns:
        _safe_add_column("chat_sessions", "last_action_target", "TEXT")
    if "last_time_window_minutes" not in session_columns:
        _safe_add_column("chat_sessions", "last_time_window_minutes", "INTEGER")
    if "pending_intent" not in session_columns:
        _safe_add_column("chat_sessions", "pending_intent", "TEXT")
    if "pending_missing_fields" not in session_columns:
        _safe_add_column("chat_sessions", "pending_missing_fields", "TEXT")
    if "pending_question" not in session_columns:
        _safe_add_column("chat_sessions", "pending_question", "TEXT")
    if "pending_options" not in session_columns:
        _safe_add_column("chat_sessions", "pending_options", "TEXT")

    # Commercial deployments are intentionally single-workspace for now. This
    # keeps all existing operational data isolated at the deployment boundary,
    # while allowing revocable keys and durable usage metering without claiming
    # cross-customer row-level isolation that the current schema does not provide.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS workspaces (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        plan TEXT NOT NULL,
        status TEXT NOT NULL,
        subscription_status TEXT,
        trial_ends_at TEXT,
        current_period_end TEXT,
        subscription_updated_at TEXT,
        monthly_request_limit INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    _safe_add_column("workspaces", "subscription_status", "TEXT")
    _safe_add_column("workspaces", "trial_ends_at", "TEXT")
    _safe_add_column("workspaces", "current_period_end", "TEXT")
    _safe_add_column("workspaces", "subscription_updated_at", "TEXT")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscription_events (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        previous_state_json TEXT,
        new_state_json TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_subscription_events_workspace_time
    ON subscription_events(workspace_id, created_at)
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS workspace_api_keys (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        key_prefix TEXT NOT NULL,
        key_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at TEXT
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_workspace_api_keys_workspace
    ON workspace_api_keys(workspace_id, revoked_at)
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_events (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        metric TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        route TEXT,
        status_code INTEGER,
        request_id TEXT UNIQUE,
        metadata_json TEXT,
        occurred_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_usage_events_workspace_time
    ON usage_events(workspace_id, occurred_at)
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pilot_outcomes (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        category TEXT NOT NULL,
        incident_id TEXT,
        change_request_id TEXT,
        service_name TEXT,
        baseline_minutes INTEGER,
        actual_minutes INTEGER,
        support_minutes INTEGER NOT NULL DEFAULT 0,
        recommendation_accepted INTEGER,
        successful INTEGER,
        notes TEXT,
        recorded_by TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(workspace_id, idempotency_key)
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_pilot_outcomes_workspace_time
    ON pilot_outcomes(workspace_id, occurred_at)
    """)
    _safe_add_column("usage_events", "metadata_json", "TEXT")

    workspace_id = configured_workspace_id()
    workspace_name = os.getenv("SRE_WORKSPACE_NAME", "Default workspace").strip() or "Default workspace"
    workspace_plan = os.getenv("SRE_PLAN", "trial").strip().lower() or "trial"
    default_limits = {"trial": 1000, "starter": 10000, "team": 100000, "enterprise": 0}
    if workspace_plan not in default_limits:
        workspace_plan = "trial"
    try:
        request_limit = int(
            os.getenv(
                "SRE_MONTHLY_REQUEST_LIMIT",
                str(default_limits.get(workspace_plan, 1000)),
            )
        )
    except ValueError:
        request_limit = default_limits.get(workspace_plan, 1000)
    now_value = datetime.now(timezone.utc)
    now = now_value.isoformat()
    try:
        trial_days = int(os.getenv("SRE_TRIAL_DAYS", "14"))
    except ValueError as exc:
        raise RuntimeError("SRE_TRIAL_DAYS must be an integer between 1 and 3650") from exc
    if not 1 <= trial_days <= 3650:
        raise RuntimeError("SRE_TRIAL_DAYS must be an integer between 1 and 3650")

    def _configured_datetime(name: str) -> tuple[bool, str | None]:
        raw = os.getenv(name, "").strip()
        if not raw:
            return False, None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an ISO-8601 datetime") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return True, parsed.astimezone(timezone.utc).isoformat()

    explicit_trial_end, configured_trial_end = _configured_datetime("SRE_TRIAL_ENDS_AT")
    explicit_period_end, configured_period_end = _configured_datetime("SRE_CURRENT_PERIOD_END")
    raw_subscription_status = os.getenv("SRE_SUBSCRIPTION_STATUS", "").strip().lower()
    valid_subscription_statuses = {
        "trialing",
        "active",
        "past_due",
        "suspended",
        "canceled",
        "expired",
    }
    if raw_subscription_status and raw_subscription_status not in valid_subscription_statuses:
        raise RuntimeError(
            "SRE_SUBSCRIPTION_STATUS must be trialing, active, past_due, suspended, canceled, or expired"
        )

    cur.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
    existing_row = cur.fetchone()
    existing = dict(existing_row) if existing_row else None
    plan_changed = bool(existing and existing.get("plan") != workspace_plan)
    default_subscription_status = "trialing" if workspace_plan == "trial" else "active"
    if raw_subscription_status:
        subscription_status = raw_subscription_status
    elif existing and not plan_changed and existing.get("subscription_status"):
        subscription_status = existing["subscription_status"]
    else:
        subscription_status = default_subscription_status

    if workspace_plan == "trial":
        if explicit_trial_end:
            trial_ends_at = configured_trial_end
        elif existing and not plan_changed and existing.get("trial_ends_at"):
            trial_ends_at = existing["trial_ends_at"]
        else:
            trial_ends_at = (now_value + timedelta(days=trial_days)).isoformat()
    else:
        trial_ends_at = None

    if explicit_period_end:
        current_period_end = configured_period_end
    elif existing and not plan_changed:
        current_period_end = existing.get("current_period_end")
    else:
        current_period_end = None

    new_subscription_state = {
        "plan": workspace_plan,
        "subscription_status": subscription_status,
        "trial_ends_at": trial_ends_at,
        "current_period_end": current_period_end,
        "monthly_request_limit": max(0, request_limit),
    }
    previous_subscription_state = None
    if existing:
        previous_subscription_state = {
            key: existing.get(key) for key in new_subscription_state
        }
    subscription_changed = previous_subscription_state != new_subscription_state
    subscription_updated_at = (
        now
        if subscription_changed
        else (existing or {}).get("subscription_updated_at") or now
    )

    cur.execute(
        """
        INSERT INTO workspaces (
            id, name, plan, status, subscription_status, trial_ends_at,
            current_period_end, subscription_updated_at,
            monthly_request_limit, created_at, updated_at
        ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            plan = excluded.plan,
            subscription_status = excluded.subscription_status,
            trial_ends_at = excluded.trial_ends_at,
            current_period_end = excluded.current_period_end,
            subscription_updated_at = excluded.subscription_updated_at,
            monthly_request_limit = excluded.monthly_request_limit,
            updated_at = excluded.updated_at
        """,
        (
            workspace_id,
            workspace_name,
            workspace_plan,
            subscription_status,
            trial_ends_at,
            current_period_end,
            subscription_updated_at,
            max(0, request_limit),
            now,
            now,
        ),
    )
    if subscription_changed:
        cur.execute(
            """
            INSERT INTO subscription_events (
                id, workspace_id, previous_state_json, new_state_json,
                actor, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                json.dumps(previous_subscription_state, ensure_ascii=False, sort_keys=True)
                if previous_subscription_state
                else None,
                json.dumps(new_subscription_state, ensure_ascii=False, sort_keys=True),
                "deployment-configuration",
                "workspace subscription configuration reconciled",
                now,
            ),
        )

def _ensure_migration_table(cur) -> None:
    cur.execute("""
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """)


def _validate_and_record_migrations(cur) -> None:
    cur.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version ASC"
    )
    applied = {int(row["version"]): row for row in cur.fetchall()}
    if applied and max(applied) > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            "database schema is newer than this application; deploy a compatible application version"
        )

    now = datetime.now(timezone.utc).isoformat()
    for version, name, fingerprint in MIGRATION_MANIFEST:
        expected_checksum = _migration_checksum(version, name, fingerprint)
        existing = applied.get(version)
        if existing:
            if existing["name"] != name or not hmac.compare_digest(
                existing["checksum"], expected_checksum
            ):
                raise RuntimeError(f"schema migration {version} checksum mismatch")
            continue
        cur.execute(
            """
            INSERT INTO schema_migrations (version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (version, name, expected_checksum, now),
        )


def init_db() -> None:
    """Upgrade the schema atomically and reject incompatible database versions."""
    conn = get_conn()
    cur = conn.cursor()
    postgres = is_postgres_database()
    try:
        if postgres:
            cur.execute("SELECT pg_advisory_lock(?)", (_POSTGRES_MIGRATION_LOCK_ID,))
        else:
            cur.execute("BEGIN IMMEDIATE")
        _ensure_migration_table(cur)
        _validate_and_record_migrations(cur)
        _initialize_schema(conn, cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if postgres:
            try:
                cur.execute("SELECT pg_advisory_unlock(?)", (_POSTGRES_MIGRATION_LOCK_ID,))
                conn.commit()
            except Exception:
                conn.rollback()
        conn.close()


def get_schema_status() -> dict:
    conn = get_conn()
    cur = conn.cursor()
    try:
        _ensure_migration_table(cur)
        cur.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version ASC"
        )
        rows = [dict(row) for row in cur.fetchall()]
        applied_by_version = {int(row["version"]): row for row in rows}
        valid = True
        for version, name, fingerprint in MIGRATION_MANIFEST:
            row = applied_by_version.get(version)
            expected = _migration_checksum(version, name, fingerprint)
            if not row or row["name"] != name or not hmac.compare_digest(
                row["checksum"], expected
            ):
                valid = False
                break
        applied_version = max(applied_by_version, default=0)
        compatible = valid and applied_version == CURRENT_SCHEMA_VERSION
        conn.commit()
        return {
            "current_version": CURRENT_SCHEMA_VERSION,
            "applied_version": applied_version,
            "compatible": compatible,
            "migrations": rows,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
