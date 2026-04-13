from pathlib import Path
import os

import sqlite3

#找到当前文件（向上俩级目录）
BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv("SRE_AGENT_DB_PATH", BASE_DIR / "sre_agent.db"))

#连接数据库
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    def _safe_add_column(table_name: str, column_name: str, column_sql: str):
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
        created_at TEXT NOT NULL
    )
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

    conn.commit()
    conn.close()
