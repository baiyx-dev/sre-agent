import json
import hmac
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from backend.storage.db import (
    AUDIT_LEDGER_LOCK_ID,
    audit_ledger_entry_hash,
    canonical_audit_payload,
    get_conn,
    is_postgres_database,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _change_request_from_row(row) -> dict | None:
    if not row:
        return None
    return {
        "change_request_id": row["id"],
        "action_type": row["action_type"],
        "service_name": row["service_name"],
        "target_version": row["target_version"],
        "policy_decision": _parse_json_object(row["policy_json"]),
        "resolved_entities": _parse_json_object(row["resolved_entities_json"]),
        "status": row["status"],
        "requested_by": row["requested_by"],
        "approved_by": row["approved_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "confirmed_at": row["confirmed_at"],
        "completed_at": row["completed_at"],
        "result": _parse_json_object(row["result_json"]),
    }


def _change_job_from_row(row) -> dict | None:
    if not row:
        return None
    return {
        "job_id": row["id"],
        "change_request_id": row["change_request_id"],
        "status": row["status"],
        "dry_run": bool(row["dry_run"]),
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "available_at": row["available_at"],
        "locked_by": row["locked_by"],
        "locked_at": row["locked_at"],
        "last_error": row["last_error"],
        "redrive_count": row["redrive_count"],
        "redriven_by": row["redriven_by"],
        "redriven_at": row["redriven_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def create_change_request(
    action_type: str,
    service_name: str,
    target_version: str | None,
    policy_decision: dict,
    resolved_entities: dict | None = None,
    ttl_seconds: int = 900,
    requested_by: str = "system",
) -> dict:
    if action_type not in {"deploy", "rollback"}:
        raise ValueError(f"unsupported change action: {action_type}")
    if not service_name:
        raise ValueError("service_name is required")

    now = _utc_now()
    expires_at = now + timedelta(seconds=max(1, ttl_seconds))
    change_request_id = str(uuid.uuid4())
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO change_requests (
            id, action_type, service_name, target_version,
            policy_json, resolved_entities_json, status,
            requested_by, created_at, expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            change_request_id,
            action_type,
            service_name,
            target_version,
            json.dumps(policy_decision or {}, ensure_ascii=False),
            json.dumps(resolved_entities or {}, ensure_ascii=False),
            requested_by,
            now.isoformat(),
            expires_at.isoformat(),
        ),
    )
    conn.commit()
    cur.execute("SELECT * FROM change_requests WHERE id = ?", (change_request_id,))
    result = _change_request_from_row(cur.fetchone())
    conn.close()
    return result or {}


def get_change_request(change_request_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM change_requests WHERE id = ?", (change_request_id,))
    result = _change_request_from_row(cur.fetchone())
    conn.close()
    return result


def claim_change_request(
    change_request_id: str,
    approved_by: str = "system",
) -> tuple[dict | None, str | None]:
    """Atomically move one pending request to executing.

    The conditional update provides replay protection across concurrent workers.
    """
    now = _utc_now().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(
        """
        UPDATE change_requests
        SET status = 'executing', confirmed_at = ?, approved_by = ?
        WHERE id = ? AND status = 'pending' AND expires_at > ?
        """,
        (now, approved_by, change_request_id, now),
    )
    claimed = cur.rowcount == 1
    cur.execute("SELECT * FROM change_requests WHERE id = ?", (change_request_id,))
    row = cur.fetchone()

    if claimed:
        conn.commit()
        result = _change_request_from_row(row)
        conn.close()
        return result, None

    if row and row["status"] == "pending" and row["expires_at"] <= now:
        cur.execute(
            "UPDATE change_requests SET status = 'expired', completed_at = ? WHERE id = ? AND status = 'pending'",
            (now, change_request_id),
        )
        conn.commit()
        conn.close()
        return None, "change_request_expired"

    conn.rollback()
    conn.close()
    if not row:
        return None, "change_request_not_found"
    return None, f"change_request_not_pending:{row['status']}"


def complete_change_request(change_request_id: str, status: str, result: dict) -> None:
    if status not in {"executed", "dry_run", "denied", "failed"}:
        raise ValueError(f"unsupported completion status: {status}")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE change_requests
        SET status = ?, completed_at = ?, result_json = ?
        WHERE id = ? AND status = 'executing'
        """,
        (
            status,
            _utc_now().isoformat(),
            json.dumps(result or {}, ensure_ascii=False),
            change_request_id,
        ),
    )
    conn.commit()
    conn.close()


def enqueue_change_request(
    change_request_id: str,
    *,
    approved_by: str,
    dry_run: bool,
    max_attempts: int = 1,
) -> tuple[dict | None, str | None]:
    now = _utc_now().isoformat()
    job_id = str(uuid.uuid4())
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(
        """
        UPDATE change_requests
        SET status = 'queued', confirmed_at = ?, approved_by = ?
        WHERE id = ? AND status = 'pending' AND expires_at > ?
        """,
        (now, approved_by, change_request_id, now),
    )
    enqueued = cur.rowcount == 1
    cur.execute("SELECT * FROM change_requests WHERE id = ?", (change_request_id,))
    row = cur.fetchone()
    if enqueued:
        cur.execute(
            """
            INSERT INTO change_jobs (
                id, change_request_id, status, dry_run, attempts, max_attempts,
                available_at, created_at, updated_at
            )
            VALUES (?, ?, 'queued', ?, 0, ?, ?, ?, ?)
            """,
            (
                job_id,
                change_request_id,
                1 if dry_run else 0,
                max(1, min(max_attempts, 10)),
                now,
                now,
                now,
            ),
        )
        conn.commit()
        result = _change_request_from_row(row)
        conn.close()
        return result, None

    if row and row["status"] == "pending" and row["expires_at"] <= now:
        cur.execute(
            "UPDATE change_requests SET status = 'expired', completed_at = ? WHERE id = ? AND status = 'pending'",
            (now, change_request_id),
        )
        conn.commit()
        conn.close()
        return None, "change_request_expired"

    conn.rollback()
    conn.close()
    if not row:
        return None, "change_request_not_found"
    return None, f"change_request_not_pending:{row['status']}"


def get_change_job(change_request_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM change_jobs WHERE change_request_id = ?",
        (change_request_id,),
    )
    result = _change_job_from_row(cur.fetchone())
    conn.close()
    return result


def get_change_queue_metrics() -> dict:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM change_jobs
        GROUP BY status
        """
    )
    counts = {row["status"]: row["count"] for row in cur.fetchall()}
    conn.close()
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
        "unknown": counts.get("unknown", 0),
        "cancelled": counts.get("cancelled", 0),
    }


def touch_worker_heartbeat(
    worker_id: str,
    *,
    hostname: str,
    process_id: int,
    status: str,
    started_at: str,
) -> None:
    if status not in {"starting", "polling", "idle", "stopped"}:
        raise ValueError("unsupported worker heartbeat status")
    now = _utc_now().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO worker_heartbeats (
                worker_id, hostname, process_id, status, started_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                hostname = excluded.hostname,
                process_id = excluded.process_id,
                status = excluded.status,
                last_seen_at = excluded.last_seen_at
            """,
            (worker_id, hostname, int(process_id), status, started_at, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def worker_heartbeat_status(*, now: datetime | None = None) -> dict:
    try:
        max_age_seconds = int(
            os.getenv("SRE_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "90").strip()
        )
    except ValueError:
        max_age_seconds = 90
    max_age_seconds = max(10, min(max_age_seconds, 3600))
    current = now or _utc_now()
    cutoff = current - timedelta(seconds=max_age_seconds)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT worker_id, hostname, process_id, status, started_at, last_seen_at
        FROM worker_heartbeats
        ORDER BY last_seen_at DESC
        """
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    active_workers = []
    stale_workers = []
    for worker in rows:
        try:
            last_seen_at = datetime.fromisoformat(worker["last_seen_at"])
            if last_seen_at.tzinfo is None:
                last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            stale_workers.append(worker)
            continue
        if worker["status"] != "stopped" and last_seen_at >= cutoff:
            active_workers.append(worker)
        else:
            stale_workers.append(worker)

    return {
        "healthy": bool(active_workers),
        "max_age_seconds": max_age_seconds,
        "active_count": len(active_workers),
        "stale_count": len(stale_workers),
        "latest_worker": rows[0] if rows else None,
        "active_workers": active_workers,
    }


def claim_next_change_job(
    worker_id: str,
    *,
    lease_seconds: int = 300,
) -> tuple[dict | None, dict | None]:
    now_dt = _utc_now()
    now = now_dt.isoformat()
    stale_before = (now_dt - timedelta(seconds=max(30, lease_seconds))).isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")

    cur.execute(
        """
        UPDATE change_jobs
        SET status = 'unknown', locked_by = NULL, locked_at = NULL,
            last_error = 'worker_lease_expired_execution_unknown',
            updated_at = ?, completed_at = ?
        WHERE status = 'running' AND locked_at < ? AND attempts >= max_attempts
        """,
        (now, now, stale_before),
    )
    cur.execute(
        """
        UPDATE change_requests
        SET status = 'unknown', completed_at = ?,
            result_json = '{"error":"worker_lease_expired_execution_unknown"}'
        WHERE id IN (
            SELECT change_request_id FROM change_jobs
            WHERE status = 'unknown' AND last_error = 'worker_lease_expired_execution_unknown'
        ) AND status = 'running'
        """,
        (now,),
    )
    cur.execute(
        """
        UPDATE change_jobs
        SET status = 'queued', locked_by = NULL, locked_at = NULL,
            last_error = 'worker_lease_expired', available_at = ?, updated_at = ?
        WHERE status = 'running' AND locked_at < ? AND attempts < max_attempts
        """,
        (now, now, stale_before),
    )
    cur.execute(
        """
        UPDATE change_requests
        SET status = 'queued'
        WHERE id IN (
            SELECT change_request_id FROM change_jobs
            WHERE status = 'queued' AND last_error = 'worker_lease_expired'
        ) AND status = 'running'
        """
    )
    cur.execute(
        """
        SELECT * FROM change_jobs
        WHERE status = 'queued' AND available_at <= ? AND attempts < max_attempts
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (now,),
    )
    job_row = cur.fetchone()
    if not job_row:
        conn.commit()
        conn.close()
        return None, None

    cur.execute(
        """
        UPDATE change_jobs
        SET status = 'running', locked_by = ?, locked_at = ?,
            attempts = attempts + 1, updated_at = ?
        WHERE id = ? AND status = 'queued'
        """,
        (worker_id, now, now, job_row["id"]),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return None, None
    cur.execute(
        "UPDATE change_requests SET status = 'running' WHERE id = ? AND status = 'queued'",
        (job_row["change_request_id"],),
    )
    cur.execute("SELECT * FROM change_jobs WHERE id = ?", (job_row["id"],))
    claimed_job = _change_job_from_row(cur.fetchone())
    cur.execute(
        "SELECT * FROM change_requests WHERE id = ?",
        (job_row["change_request_id"],),
    )
    change_request = _change_request_from_row(cur.fetchone())
    conn.commit()
    conn.close()
    return claimed_job, change_request


def complete_change_job(
    job_id: str,
    change_request_id: str,
    *,
    completion_status: str,
    result: dict,
) -> None:
    if completion_status not in {"executed", "dry_run", "denied", "failed"}:
        raise ValueError(f"unsupported completion status: {completion_status}")
    now = _utc_now().isoformat()
    job_status = "succeeded" if completion_status in {"executed", "dry_run"} else completion_status
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(
        """
        UPDATE change_jobs
        SET status = ?, updated_at = ?, completed_at = ?,
            locked_by = NULL, locked_at = NULL
        WHERE id = ? AND status = 'running'
        """,
        (job_status, now, now, job_id),
    )
    cur.execute(
        """
        UPDATE change_requests
        SET status = ?, completed_at = ?, result_json = ?
        WHERE id = ? AND status = 'running'
        """,
        (
            completion_status,
            now,
            json.dumps(result or {}, ensure_ascii=False),
            change_request_id,
        ),
    )
    conn.commit()
    conn.close()


def retry_or_fail_change_job(
    job_id: str,
    change_request_id: str,
    *,
    error_message: str,
    retry_delay_seconds: int = 5,
) -> bool:
    now_dt = _utc_now()
    now = now_dt.isoformat()
    available_at = (now_dt + timedelta(seconds=max(1, retry_delay_seconds))).isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute("SELECT attempts, max_attempts FROM change_jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    should_retry = bool(row and row["attempts"] < row["max_attempts"])
    if should_retry:
        cur.execute(
            """
            UPDATE change_jobs
            SET status = 'queued', available_at = ?, updated_at = ?,
                locked_by = NULL, locked_at = NULL, last_error = ?
            WHERE id = ? AND status = 'running'
            """,
            (available_at, now, error_message[:500], job_id),
        )
        cur.execute(
            "UPDATE change_requests SET status = 'queued' WHERE id = ? AND status = 'running'",
            (change_request_id,),
        )
    else:
        failure_result = {"error": "worker_execution_state_unknown", "detail": error_message[:500]}
        cur.execute(
            """
            UPDATE change_jobs
            SET status = 'unknown', updated_at = ?, completed_at = ?,
                locked_by = NULL, locked_at = NULL, last_error = ?
            WHERE id = ? AND status = 'running'
            """,
            (now, now, error_message[:500], job_id),
        )
        cur.execute(
            """
            UPDATE change_requests
            SET status = 'unknown', completed_at = ?, result_json = ?
            WHERE id = ? AND status = 'running'
            """,
            (now, json.dumps(failure_result), change_request_id),
        )
    conn.commit()
    conn.close()
    return should_retry


def redrive_failed_change_job(
    change_request_id: str,
    *,
    redriven_by: str,
    reason: str,
    max_redrives: int = 3,
) -> tuple[dict | None, dict | None, str | None]:
    now = _utc_now().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(
        "SELECT * FROM change_jobs WHERE change_request_id = ?",
        (change_request_id,),
    )
    job_row = cur.fetchone()
    cur.execute("SELECT * FROM change_requests WHERE id = ?", (change_request_id,))
    request_row = cur.fetchone()
    if not job_row or not request_row:
        conn.rollback()
        conn.close()
        return None, None, "change_job_not_found"
    if job_row["status"] not in {"failed", "unknown"} or request_row["status"] not in {
        "failed",
        "unknown",
    }:
        conn.rollback()
        conn.close()
        return None, None, "change_job_not_redrivable"
    redrive_count = int(job_row["redrive_count"] or 0)
    if redrive_count >= max(1, max_redrives):
        conn.rollback()
        conn.close()
        return None, None, "change_job_redrive_limit_reached"

    message = f"manual_redrive:{reason.strip()[:450] or 'operator_requested'}"
    cur.execute(
        """
        UPDATE change_jobs
        SET status = 'queued', max_attempts = attempts + 1,
            available_at = ?, updated_at = ?, completed_at = NULL,
            locked_by = NULL, locked_at = NULL, last_error = ?,
            redrive_count = redrive_count + 1, redriven_by = ?, redriven_at = ?
        WHERE id = ? AND status IN ('failed', 'unknown') AND redrive_count = ?
        """,
        (
            now,
            now,
            message,
            redriven_by,
            now,
            job_row["id"],
            redrive_count,
        ),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return None, None, "change_job_redrive_conflict"
    cur.execute(
        """
        UPDATE change_requests
        SET status = 'queued', completed_at = NULL,
            result_json = ?
        WHERE id = ? AND status IN ('failed', 'unknown')
        """,
        (
            json.dumps(
                {
                    "redrive_requested_by": redriven_by,
                    "redrive_reason": reason.strip()[:500],
                    "redrive_count": redrive_count + 1,
                },
                ensure_ascii=False,
            ),
            change_request_id,
        ),
    )
    cur.execute("SELECT * FROM change_jobs WHERE id = ?", (job_row["id"],))
    updated_job = _change_job_from_row(cur.fetchone())
    cur.execute("SELECT * FROM change_requests WHERE id = ?", (change_request_id,))
    updated_request = _change_request_from_row(cur.fetchone())
    conn.commit()
    conn.close()
    return updated_job, updated_request, None


def cancel_change_request(
    change_request_id: str,
    *,
    cancelled_by: str,
    reason: str,
) -> tuple[dict | None, str | None]:
    now = _utc_now().isoformat()
    result = {"cancelled_by": cancelled_by, "reason": reason}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(
        """
        UPDATE change_requests
        SET status = 'cancelled', completed_at = ?, result_json = ?
        WHERE id = ? AND status IN ('pending', 'queued')
        """,
        (now, json.dumps(result, ensure_ascii=False), change_request_id),
    )
    cancelled = cur.rowcount == 1
    cur.execute("SELECT * FROM change_requests WHERE id = ?", (change_request_id,))
    row = cur.fetchone()
    if cancelled:
        cur.execute(
            """
            UPDATE change_jobs
            SET status = 'cancelled', updated_at = ?, completed_at = ?
            WHERE change_request_id = ? AND status = 'queued'
            """,
            (now, now, change_request_id),
        )
        conn.commit()
        result_row = _change_request_from_row(row)
        conn.close()
        return result_row, None
    conn.rollback()
    conn.close()
    if not row:
        return None, "change_request_not_found"
    return None, f"change_request_not_cancellable:{row['status']}"


def list_change_requests(status: str | None = None, limit: int = 100) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    safe_limit = max(1, min(limit, 500))
    if status:
        cur.execute(
            "SELECT * FROM change_requests WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, safe_limit),
        )
    else:
        cur.execute(
            "SELECT * FROM change_requests ORDER BY created_at DESC LIMIT ?",
            (safe_limit,),
        )
    rows = cur.fetchall()
    conn.close()
    return [_change_request_from_row(row) for row in rows]


def save_task_run(user_message: str, result: dict) -> int:
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    insert_sql = """
        INSERT INTO task_runs (
            user_message, intent, final_answer,
            generation_source, llm_provider, used_fallback, fallback_reason,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
    if is_postgres_database():
        insert_sql += " RETURNING id"
    cur.execute(
        insert_sql,
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
    task_run_id = cur.fetchone()["id"] if is_postgres_database() else cur.lastrowid

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



def save_execution_audit(
    action: str,
    service_name: str | None,
    source: str,
    status: str,
    reason: str | None = None,
    actor: str = "system",
    change_request_id: str | None = None,
):
    conn = get_conn()
    cur = conn.cursor()
    now = _utc_now().isoformat()
    event_id = str(uuid.uuid4())
    values = {
        "event_id": event_id,
        "action": action,
        "service_name": service_name,
        "source": source,
        "status": status,
        "reason": reason,
        "actor": actor,
        "change_request_id": change_request_id,
        "created_at": now,
    }
    try:
        if is_postgres_database():
            cur.execute("BEGIN")
            cur.execute("SELECT pg_advisory_xact_lock(?)", (AUDIT_LEDGER_LOCK_ID,))
        else:
            cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            """
            INSERT INTO execution_audits (
                event_id, action, service_name, source, status, reason,
                actor, change_request_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, action, service_name, source, status, reason,
                actor, change_request_id, now,
            ),
        )
        cur.execute(
            "SELECT entry_hash FROM audit_ledger ORDER BY sequence DESC LIMIT 1"
        )
        head = cur.fetchone()
        if head:
            previous_hash = head["entry_hash"]
        else:
            cur.execute(
                "SELECT head_hash FROM audit_ledger_checkpoints ORDER BY id DESC LIMIT 1"
            )
            checkpoint = cur.fetchone()
            previous_hash = checkpoint["head_hash"] if checkpoint else ""
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
                event_id, action, service_name, source, status, reason, actor,
                change_request_id, now, previous_hash, entry_hash, payload_json,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_execution_audits(
    *,
    action: str | None = None,
    status: str | None = None,
    service_name: str | None = None,
    actor: str | None = None,
    before_sequence: int | None = None,
    limit: int = 100,
) -> list[dict]:
    filters = []
    values: list[object] = []
    for column, value in (
        ("action", action),
        ("status", status),
        ("service_name", service_name),
        ("actor", actor),
    ):
        if value:
            filters.append(f"{column} = ?")
            values.append(value)
    if before_sequence is not None:
        filters.append("sequence < ?")
        values.append(max(1, int(before_sequence)))
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    values.append(max(1, min(limit, 500)))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT sequence AS id, sequence, event_id, action, service_name, source,
               status, reason, actor, change_request_id, created_at,
               previous_hash, entry_hash
        FROM audit_ledger
        {where_clause}
        ORDER BY sequence DESC
        LIMIT ?
        """,
        tuple(values),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def verify_audit_ledger() -> dict:
    """Verify ordering, payload integrity, and the complete hash chain."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT pruned_through_sequence, pruned_entry_count, head_hash, created_at
        FROM audit_ledger_checkpoints
        ORDER BY id DESC
        LIMIT 1
        """
    )
    checkpoint_row = cur.fetchone()
    checkpoint = dict(checkpoint_row) if checkpoint_row else None
    cur.execute(
        """
        SELECT sequence, event_id, action, service_name, source, status, reason,
               actor, change_request_id, created_at, previous_hash,
               entry_hash, payload_json
        FROM audit_ledger
        ORDER BY sequence ASC
        """
    )
    rows = cur.fetchall()
    conn.close()

    previous_hash = checkpoint["head_hash"] if checkpoint else ""
    pruned_entry_count = checkpoint["pruned_entry_count"] if checkpoint else 0
    for verified_count, row in enumerate(rows):
        values = dict(row)
        payload_json = canonical_audit_payload(values)
        expected_hash = audit_ledger_entry_hash(previous_hash, payload_json)
        if not hmac.compare_digest(
            values["payload_json"].encode("utf-8"), payload_json.encode("utf-8")
        ):
            return {
                "valid": False,
                "entry_count": len(rows),
                "pruned_entry_count": pruned_entry_count,
                "verified_count": verified_count,
                "head_hash": previous_hash or None,
                "failure": {
                    "sequence": values["sequence"],
                    "reason": "payload_mismatch",
                },
                "checked_at": _utc_now().isoformat(),
            }
        if not hmac.compare_digest(values["previous_hash"], previous_hash):
            return {
                "valid": False,
                "entry_count": len(rows),
                "pruned_entry_count": pruned_entry_count,
                "verified_count": verified_count,
                "head_hash": previous_hash or None,
                "failure": {
                    "sequence": values["sequence"],
                    "reason": "previous_hash_mismatch",
                },
                "checked_at": _utc_now().isoformat(),
            }
        if not hmac.compare_digest(values["entry_hash"], expected_hash):
            return {
                "valid": False,
                "entry_count": len(rows),
                "pruned_entry_count": pruned_entry_count,
                "verified_count": verified_count,
                "head_hash": previous_hash or None,
                "failure": {
                    "sequence": values["sequence"],
                    "reason": "entry_hash_mismatch",
                },
                "checked_at": _utc_now().isoformat(),
            }
        previous_hash = values["entry_hash"]

    return {
        "valid": True,
        "entry_count": len(rows),
        "pruned_entry_count": pruned_entry_count,
        "verified_count": len(rows),
        "head_hash": previous_hash or None,
        "checkpoint": checkpoint,
        "failure": None,
        "checked_at": _utc_now().isoformat(),
    }
