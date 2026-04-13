import json
import re
from datetime import datetime

from backend.storage.db import get_conn


def save_task_run(user_message: str, result: dict) -> int:
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        """
        INSERT INTO task_runs (
            user_message, intent, final_answer,
            generation_source, llm_provider, used_fallback, fallback_reason,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_message,
            result.get("intent", "unknown"),
            result.get("final_answer", ""),
            result.get("generation_source", "fallback_rule"),
            result.get("llm_provider", "deepseek"),
            1 if result.get("used_fallback", True) else 0,
            result.get("fallback_reason", "rule_only"),
            now,
        ),
    )
    task_run_id = cur.lastrowid

    steps = result.get("steps", [])
    for idx, step in enumerate(steps, start=1):
        step_no = step.get("step", idx)
        action = step.get("action", "")
        result_json = json.dumps(step.get("result"), ensure_ascii=False)
        cur.execute(
            """
            INSERT INTO task_steps (task_run_id, step_no, action, result_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_run_id, step_no, action, result_json, now),
        )

    conn.commit()
    conn.close()
    return task_run_id


def get_app_setting(key: str) -> str | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else None


def set_app_setting(key: str, value: str | None):
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO app_settings(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, value, now),
    )
    conn.commit()
    conn.close()


def get_chat_session_context(session_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            session_id,
            last_service_name,
            last_intent,
            last_version,
            last_env,
            last_namespace,
            last_cluster,
            last_region,
            last_action_target,
            last_time_window_minutes,
            pending_intent,
            pending_missing_fields,
            pending_question,
            pending_options,
            updated_at
        FROM chat_sessions
        WHERE session_id = ?
        """,
        (session_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_chat_session_context(
    session_id: str,
    service_name: str | None = None,
    intent: str | None = None,
    version: str | None = None,
    env: str | None = None,
    namespace: str | None = None,
    cluster: str | None = None,
    region: str | None = None,
    action_target: str | None = None,
    time_window_minutes: int | None = None,
    pending_intent: str | None = None,
    pending_missing_fields: list[str] | None = None,
    pending_question: str | None = None,
    pending_options: list[str] | None = None,
    clear_pending: bool = False,
):
    existing = get_chat_session_context(session_id) or {}
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO chat_sessions(
            session_id,
            last_service_name,
            last_intent,
            last_version,
            last_env,
            last_namespace,
            last_cluster,
            last_region,
            last_action_target,
            last_time_window_minutes,
            pending_intent,
            pending_missing_fields,
            pending_question,
            pending_options,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            last_service_name=excluded.last_service_name,
            last_intent=excluded.last_intent,
            last_version=excluded.last_version,
            last_env=excluded.last_env,
            last_namespace=excluded.last_namespace,
            last_cluster=excluded.last_cluster,
            last_region=excluded.last_region,
            last_action_target=excluded.last_action_target,
            last_time_window_minutes=excluded.last_time_window_minutes,
            pending_intent=excluded.pending_intent,
            pending_missing_fields=excluded.pending_missing_fields,
            pending_question=excluded.pending_question,
            pending_options=excluded.pending_options,
            updated_at=excluded.updated_at
        """,
        (
            session_id,
            service_name if service_name is not None else existing.get("last_service_name"),
            intent if intent is not None else existing.get("last_intent"),
            version if version is not None else existing.get("last_version"),
            env if env is not None else existing.get("last_env"),
            namespace if namespace is not None else existing.get("last_namespace"),
            cluster if cluster is not None else existing.get("last_cluster"),
            region if region is not None else existing.get("last_region"),
            action_target if action_target is not None else existing.get("last_action_target"),
            time_window_minutes if time_window_minutes is not None else existing.get("last_time_window_minutes"),
            None if clear_pending else (pending_intent if pending_intent is not None else existing.get("pending_intent")),
            None if clear_pending else json.dumps(pending_missing_fields, ensure_ascii=False) if pending_missing_fields is not None else existing.get("pending_missing_fields"),
            None if clear_pending else (pending_question if pending_question is not None else existing.get("pending_question")),
            None if clear_pending else json.dumps(pending_options, ensure_ascii=False) if pending_options is not None else existing.get("pending_options"),
            now,
        ),
    )
    conn.commit()
    conn.close()


def list_monitored_targets() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, name, base_url, created_at
        FROM monitored_targets
        ORDER BY id DESC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_monitored_target(name: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, name, base_url, created_at
        FROM monitored_targets
        WHERE name = ?
        """,
        (name,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_monitored_target(name: str, base_url: str) -> dict:
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO monitored_targets(name, base_url, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            base_url=excluded.base_url
        """,
        (name, base_url, now),
    )
    conn.commit()
    cur.execute(
        "SELECT id, name, base_url, created_at FROM monitored_targets WHERE name = ?",
        (name,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row)


def delete_monitored_target(name: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM monitored_targets WHERE name = ?", (name,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted



def get_task_timeline(limit: int = 20) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, user_message, intent, final_answer, created_at
        FROM task_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    runs = cur.fetchall()

    timeline = []
    for run in runs:
        cur.execute(
            """
            SELECT step_no, action, result_json, created_at
            FROM task_steps
            WHERE task_run_id = ?
            ORDER BY step_no ASC
            """,
            (run["id"],),
        )
        raw_steps = cur.fetchall()

        steps = []
        for step in raw_steps:
            try:
                parsed_result = json.loads(step["result_json"])
            except Exception:
                parsed_result = step["result_json"]

            steps.append({
                "step_no": step["step_no"],
                "action": step["action"],
                "result": parsed_result,
                "created_at": step["created_at"],
            })

        timeline.append({
            "id": run["id"],
            "user_message": run["user_message"],
            "intent": run["intent"],
            "final_answer": run["final_answer"],
            "created_at": run["created_at"],
            "steps": steps,
        })

    conn.close()
    return timeline


def get_recent_deploy_context(service_name: str, limit: int = 3) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, service, old_version, new_version, status, created_at
        FROM deployments
        WHERE service = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (service_name, limit),
    )
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": row["id"],
            "service": row["service"],
            "old_version": row["old_version"],
            "new_version": row["new_version"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _extract_service_name(user_message: str, steps: list[dict]) -> str | None:
    match = re.search(r"[a-z]+-service", user_message.lower())
    if match:
        return match.group(0)

    def find_in_result(result):
        if isinstance(result, dict):
            for key in ("service", "service_name", "name"):
                value = result.get(key)
                if isinstance(value, str) and value.endswith("-service"):
                    return value
            for value in result.values():
                nested = find_in_result(value)
                if nested:
                    return nested
        if isinstance(result, list):
            for item in result:
                nested = find_in_result(item)
                if nested:
                    return nested
        return None

    for step in steps:
        service_name = find_in_result(step.get("result"))
        if service_name:
            return service_name
    return None


def generate_postmortem(task_run_id: int, limit: int = 50) -> dict:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, user_message, intent, final_answer, created_at
        FROM task_runs
        WHERE id = ?
        """,
        (task_run_id,),
    )
    run = cur.fetchone()
    if not run:
        conn.close()
        return {
            "task_run_id": task_run_id,
            "summary": "未找到对应任务记录。",
            "service_name": None,
            "incident_type": "unknown",
            "impact": {},
            "symptoms": [],
            "likely_root_cause": "unknown",
            "actions_taken": [],
            "current_status": "unknown",
            "follow_ups": [],
        }

    cur.execute(
        """
        SELECT task_run_id, step_no, action, result_json, created_at
        FROM task_steps
        WHERE task_run_id = ?
        ORDER BY step_no ASC
        """,
        (task_run_id,),
    )
    raw_steps = cur.fetchall()

    steps = []
    for step in raw_steps:
        try:
            parsed_result = json.loads(step["result_json"])
        except Exception:
            parsed_result = step["result_json"]
        steps.append({
            "task_run_id": step["task_run_id"],
            "step_no": step["step_no"],
            "action": step["action"],
            "result": parsed_result,
            "created_at": step["created_at"],
        })

    service_name = _extract_service_name(run["user_message"], steps)
    service_filter_sql = ""
    service_filter_args = []
    if service_name:
        service_filter_sql = " WHERE service = ? "
        service_filter_args = [service_name]

    cur.execute(
        f"""
        SELECT id, service, severity, title, message, created_at, resolved
        FROM alerts
        {service_filter_sql}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(service_filter_args + [limit]),
    )
    alerts = cur.fetchall()

    cur.execute(
        f"""
        SELECT id, service, timestamp, level, message
        FROM logs
        {service_filter_sql}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(service_filter_args + [limit]),
    )
    logs = cur.fetchall()

    cur.execute(
        f"""
        SELECT id, service, old_version, new_version, status, created_at
        FROM deployments
        {service_filter_sql}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(service_filter_args + [limit]),
    )
    deployments = cur.fetchall()

    conn.close()

    alert_records = [
        {
            "id": a["id"],
            "service": a["service"],
            "severity": a["severity"],
            "title": a["title"],
            "message": a["message"],
            "created_at": a["created_at"],
            "resolved": bool(a["resolved"]),
        }
        for a in alerts
    ]
    log_records = [
        {
            "id": l["id"],
            "service": l["service"],
            "timestamp": l["timestamp"],
            "level": l["level"],
            "message": l["message"],
        }
        for l in logs
    ]
    deployment_records = [
        {
            "id": d["id"],
            "service": d["service"],
            "old_version": d["old_version"],
            "new_version": d["new_version"],
            "status": d["status"],
            "created_at": d["created_at"],
        }
        for d in deployments
    ]

    unresolved_alerts = [a for a in alert_records if not a["resolved"]]
    critical_alerts = [a for a in alert_records if a["severity"] == "critical"]
    error_logs = [l for l in log_records if l["level"] in ("ERROR", "CRITICAL")]
    timeout_logs = [l for l in log_records if "timeout" in l["message"].lower()]
    rollback_deploys = [d for d in deployment_records if "rollback" in d["status"]]
    deploy_actions = [s for s in steps if s["action"] in ("deploy_service", "rollback_service")]

    symptoms = []
    for a in alert_records[:3]:
        symptoms.append(f"alert:{a['severity']} {a['title']}")
    for l in error_logs[:3]:
        symptoms.append(f"log:{l['level']} {l['message']}")
    if not symptoms and run["final_answer"]:
        symptoms.append(run["final_answer"])

    likely_root_cause = "unknown"
    if timeout_logs:
        likely_root_cause = "downstream_or_db_timeout"
    elif run["intent"] == "deploy" and rollback_deploys:
        likely_root_cause = "deployment_regression"
    elif critical_alerts:
        likely_root_cause = "service_error_spike"

    actions_taken = [f"step:{s['action']}" for s in steps]
    if not actions_taken:
        actions_taken = [f"intent:{run['intent']}"]
    if rollback_deploys:
        actions_taken.append("system:rollback_recorded")

    incident_type = "operational_event"
    if run["intent"] == "deploy":
        incident_type = "change_failure" if rollback_deploys else "deploy_operation"
    if run["intent"] == "rollback":
        incident_type = "rollback_operation"
    if run["intent"] == "troubleshoot":
        incident_type = "service_degradation_investigation"

    current_status = "investigating"
    if run["intent"] in ("deploy", "rollback") and not unresolved_alerts and rollback_deploys:
        current_status = "mitigated_after_rollback"
    elif not unresolved_alerts and not error_logs:
        current_status = "stable"
    elif unresolved_alerts:
        current_status = "active_incident"

    follow_ups = []
    if timeout_logs:
        follow_ups.append("check_db_pool_and_downstream_timeouts")
    if unresolved_alerts:
        follow_ups.append("resolve_open_alerts_and_add_recovery_checks")
    if run["intent"] in ("deploy", "rollback"):
        follow_ups.append("add_release_guard_and_precheck")
    if not follow_ups:
        follow_ups.append("document_sop_and_continue_monitoring")

    summary = (
        f"task#{run['id']} intent={run['intent']} "
        f"alerts={len(alert_records)} errors={len(error_logs)} "
        f"steps={len(steps)} status={current_status}"
    )

    return {
        "task_run_id": run["id"],
        "summary": summary,
        "service_name": service_name,
        "incident_type": incident_type,
        "impact": {
            "alert_count": len(alert_records),
            "unresolved_alert_count": len(unresolved_alerts),
            "error_log_count": len(error_logs),
            "deployment_count": len(deployment_records),
        },
        "symptoms": symptoms,
        "likely_root_cause": likely_root_cause,
        "actions_taken": actions_taken,
        "current_status": current_status,
        "follow_ups": follow_ups,
        "evidence": {
            "task_run": {
                "id": run["id"],
                "user_message": run["user_message"],
                "intent": run["intent"],
                "final_answer": run["final_answer"],
                "created_at": run["created_at"],
            },
            "task_steps": steps[:20],
            "alerts": alert_records[:10],
            "logs": log_records[:20],
            "deployments": deployment_records[:10],
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }



def save_execution_audit(action: str, service_name: str | None, source: str, status: str, reason: str | None = None):
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO execution_audits (action, service_name, source, status, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (action, service_name, source, status, reason, now),
    )
    conn.commit()
    conn.close()
