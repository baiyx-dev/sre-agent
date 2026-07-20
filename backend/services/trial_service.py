import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from backend.services.commercial_service import get_subscription_status, get_workspace
from backend.storage.db import configured_workspace_id, get_conn, is_postgres_database


TRIAL_OUTCOMES = {"not_evaluated", "not_useful", "some_value", "high_value"}
PURCHASE_INTENTS = {"no", "maybe", "yes"}
_EVIDENCE_ACTIONS = (
    "get_service_status",
    "list_services",
    "get_service_metrics",
    "get_recent_logs",
    "get_recent_alerts",
    "get_recent_deploy_context",
    "get_k8s_observability",
)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PLACEHOLDER_TOKENS = {
    "replace_me",
    "replace-with-a-long-random-secret",
    "change-me",
    "your-token-here",
}


class TrialError(ValueError):
    pass


class TrialConfigurationError(TrialError):
    pass


class TrialActivationUnauthorized(TrialError):
    pass


class TrialActivationConflict(TrialError):
    pass


class TrialActivationRateLimited(TrialError):
    pass


class TrialFeedbackConflict(TrialError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _trial_days() -> int:
    try:
        value = int(os.getenv("SRE_TRIAL_DAYS", "14"))
    except ValueError as exc:
        raise TrialConfigurationError("SRE_TRIAL_DAYS must be an integer") from exc
    if not 1 <= value <= 3650:
        raise TrialConfigurationError("SRE_TRIAL_DAYS must be between 1 and 3650")
    return value


def _activation_token() -> str:
    return os.getenv("SRE_TRIAL_ACTIVATION_TOKEN", "").strip()


def _valid_activation_token(token: str) -> bool:
    return (
        len(token) >= 32
        and token.lower() not in _PLACEHOLDER_TOKENS
        and not token.lower().startswith("replace_with")
    )


def _upgrade_contact_url() -> str | None:
    value = os.getenv("SRE_UPGRADE_CONTACT_URL", "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme == "mailto" and parsed.path and not parsed.netloc:
        return value
    if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
        return value
    return None


def trial_activation_configuration_status() -> dict:
    enabled = _enabled("SRE_TRIAL_SELF_SERVICE_ENABLED")
    start_mode = os.getenv("SRE_TRIAL_START_MODE", "deployment").strip().lower()
    token_configured = _valid_activation_token(_activation_token())
    configured = (not enabled) or (start_mode == "activation" and token_configured)
    reasons = []
    if enabled and start_mode != "activation":
        reasons.append("SRE_TRIAL_START_MODE must be activation")
    if enabled and not token_configured:
        reasons.append("SRE_TRIAL_ACTIVATION_TOKEN must contain at least 32 non-placeholder characters")
    return {
        "enabled": enabled,
        "configured": configured,
        "start_mode": start_mode,
        "token_configured": token_configured,
        "trial_days": _trial_days(),
        "upgrade_contact_configured": bool(_upgrade_contact_url()),
        "configuration_errors": reasons,
    }


def _activation_row(cur, workspace_id: str):
    cur.execute(
        "SELECT * FROM trial_activations WHERE workspace_id = ?",
        (workspace_id,),
    )
    return cur.fetchone()


def public_trial_status() -> dict:
    configuration = trial_activation_configuration_status()
    workspace = get_workspace()
    claimed = bool(workspace and workspace.get("trial_activated_at"))
    pending = bool(
        workspace and workspace.get("subscription_status") == "pending_activation"
    )
    return {
        "self_service_enabled": configuration["enabled"],
        "configured": configuration["configured"],
        "claim_available": bool(
            configuration["enabled"]
            and configuration["configured"]
            and pending
            and not claimed
        ),
        "claimed": claimed,
        "status": "pending_activation" if pending else "active_or_managed",
        "trial_days": configuration["trial_days"],
        "requires_activation_token": bool(configuration["enabled"]),
        "paid_upgrade_available": bool(_upgrade_contact_url()),
    }


def _requester_hash(requester: str, token: str) -> str:
    return hashlib.sha256(f"{requester}|{token}".encode("utf-8")).hexdigest()


def _record_attempt(cur, requester_hash: str, success: bool, attempted_at: str) -> None:
    cur.execute(
        """
        INSERT INTO trial_activation_attempts (id, requester_hash, success, attempted_at)
        VALUES (?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), requester_hash, 1 if success else 0, attempted_at),
    )


def _enforce_activation_rate_limit(cur, requester_hash: str, now: datetime) -> None:
    cutoff = (now - timedelta(minutes=15)).isoformat()
    cur.execute(
        """
        SELECT COUNT(*) AS count
        FROM trial_activation_attempts
        WHERE requester_hash = ? AND success = 0 AND attempted_at >= ?
        """,
        (requester_hash, cutoff),
    )
    requester_failures = int(cur.fetchone()["count"])
    cur.execute(
        """
        SELECT COUNT(*) AS count
        FROM trial_activation_attempts
        WHERE success = 0 AND attempted_at >= ?
        """,
        (cutoff,),
    )
    global_failures = int(cur.fetchone()["count"])
    if requester_failures >= 5 or global_failures >= 50:
        raise TrialActivationRateLimited("too many trial activation attempts; retry later")


def activate_trial(
    *,
    activation_token: str,
    workspace_name: str,
    admin_name: str,
    contact_email: str,
    requester: str,
    now: datetime | None = None,
) -> dict:
    configuration = trial_activation_configuration_status()
    if not configuration["enabled"] or not configuration["configured"]:
        raise TrialConfigurationError("self-service trial activation is not available")
    normalized_workspace_name = workspace_name.strip()[:100]
    normalized_admin_name = admin_name.strip()[:100]
    normalized_email = contact_email.strip().lower()[:254]
    if not normalized_workspace_name:
        raise TrialError("workspace_name is required")
    if not normalized_admin_name:
        raise TrialError("admin_name is required")
    if not _EMAIL_RE.fullmatch(normalized_email):
        raise TrialError("contact_email must be a valid email address")

    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    now_iso = current.isoformat()
    expected_token = _activation_token()
    requester_fingerprint = _requester_hash(requester[:300], expected_token)
    workspace_id = configured_workspace_id()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        if is_postgres_database():
            cur.execute("SELECT id FROM workspaces WHERE id = ? FOR UPDATE", (workspace_id,))
        _enforce_activation_rate_limit(cur, requester_fingerprint, current)
        if not hmac.compare_digest(activation_token.strip(), expected_token):
            _record_attempt(cur, requester_fingerprint, False, now_iso)
            conn.commit()
            raise TrialActivationUnauthorized("invalid trial activation token")

        cur.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        workspace_row = cur.fetchone()
        if not workspace_row:
            raise TrialConfigurationError("configured workspace does not exist")
        workspace = dict(workspace_row)
        if _activation_row(cur, workspace_id) or workspace.get("trial_activated_at"):
            raise TrialActivationConflict("trial has already been activated")
        if workspace.get("plan") != "trial":
            raise TrialActivationConflict("workspace is not on the trial plan")
        if workspace.get("subscription_status") != "pending_activation":
            raise TrialActivationConflict("trial is not pending activation")

        expires_at = (current + timedelta(days=configuration["trial_days"])).isoformat()
        key_id = str(uuid.uuid4())
        raw_key = f"sre_live_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        key_prefix = raw_key[:17]
        previous_state = {
            "plan": workspace.get("plan"),
            "subscription_status": workspace.get("subscription_status"),
            "trial_activated_at": workspace.get("trial_activated_at"),
            "trial_ends_at": workspace.get("trial_ends_at"),
            "current_period_end": workspace.get("current_period_end"),
            "monthly_request_limit": workspace.get("monthly_request_limit"),
        }
        new_state = {
            **previous_state,
            "subscription_status": "trialing",
            "trial_activated_at": now_iso,
            "trial_ends_at": expires_at,
        }
        cur.execute(
            """
            UPDATE workspaces
            SET name = ?, subscription_status = 'trialing',
                trial_activated_at = ?, trial_ends_at = ?,
                subscription_updated_at = ?, updated_at = ?
            WHERE id = ? AND subscription_status = 'pending_activation'
                  AND trial_activated_at IS NULL
            """,
            (
                normalized_workspace_name,
                now_iso,
                expires_at,
                now_iso,
                now_iso,
                workspace_id,
            ),
        )
        if cur.rowcount != 1:
            raise TrialActivationConflict("trial was activated concurrently")
        cur.execute(
            """
            INSERT INTO workspace_api_keys (
                id, workspace_id, name, role, key_prefix, key_hash, created_at
            ) VALUES (?, ?, ?, 'admin', ?, ?, ?)
            """,
            (
                key_id,
                workspace_id,
                f"{normalized_admin_name} trial admin"[:100],
                key_prefix,
                key_hash,
                now_iso,
            ),
        )
        cur.execute(
            """
            INSERT INTO trial_activations (
                id, workspace_id, contact_email, admin_name,
                token_fingerprint, activated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                normalized_email,
                normalized_admin_name,
                hashlib.sha256(expected_token.encode("utf-8")).hexdigest()[:16],
                now_iso,
                expires_at,
            ),
        )
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
                json.dumps(previous_state, ensure_ascii=False, sort_keys=True),
                json.dumps(new_state, ensure_ascii=False, sort_keys=True),
                "trial-self-service",
                "free trial activated and initial workspace admin key issued",
                now_iso,
            ),
        )
        _record_attempt(cur, requester_fingerprint, True, now_iso)
        conn.commit()
    except TrialActivationUnauthorized:
        raise
    except TrialActivationRateLimited:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "workspace_id": workspace_id,
        "workspace_name": normalized_workspace_name,
        "contact_email": normalized_email,
        "activated_at": now_iso,
        "trial_ends_at": expires_at,
        "api_key": raw_key,
        "api_key_id": key_id,
        "key_prefix": key_prefix,
        "role": "admin",
        "warning": "Copy this API key now; only its hash is stored and it cannot be recovered.",
        "subscription": get_subscription_status(workspace_id, now=current),
    }


def _canonical_feedback(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_trial_feedback(
    *,
    workspace_id: str,
    idempotency_key: str,
    rating: int,
    outcome: str,
    purchase_intent: str,
    missing_feature: str | None,
    notes: str | None,
    contact_consent: bool,
    submitted_by: str,
) -> tuple[dict, bool]:
    if workspace_id != configured_workspace_id():
        raise TrialError("unknown workspace")
    normalized_key = idempotency_key.strip()[:120]
    if not normalized_key:
        raise TrialError("idempotency_key is required")
    if not 1 <= int(rating) <= 5:
        raise TrialError("rating must be between 1 and 5")
    normalized_outcome = outcome.strip().lower()
    normalized_intent = purchase_intent.strip().lower()
    if normalized_outcome not in TRIAL_OUTCOMES:
        raise TrialError("invalid trial outcome")
    if normalized_intent not in PURCHASE_INTENTS:
        raise TrialError("invalid purchase intent")
    payload = {
        "rating": int(rating),
        "outcome": normalized_outcome,
        "purchase_intent": normalized_intent,
        "missing_feature": (missing_feature or "").strip()[:1000] or None,
        "notes": (notes or "").strip()[:2000] or None,
        "contact_consent": bool(contact_consent),
    }
    payload_hash = hashlib.sha256(_canonical_feedback(payload).encode("utf-8")).hexdigest()
    now = _utc_now().isoformat()
    feedback_id = str(uuid.uuid4())
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            """
            SELECT * FROM trial_feedback
            WHERE workspace_id = ? AND idempotency_key = ?
            """,
            (workspace_id, normalized_key),
        )
        existing = cur.fetchone()
        if existing:
            if not hmac.compare_digest(existing["payload_hash"], payload_hash):
                raise TrialFeedbackConflict(
                    "idempotency_key was already used with different feedback"
                )
            conn.commit()
            return _feedback_from_row(existing), False
        cur.execute(
            """
            INSERT INTO trial_feedback (
                id, workspace_id, idempotency_key, payload_hash, rating,
                outcome, purchase_intent, missing_feature, notes,
                contact_consent, submitted_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_id,
                workspace_id,
                normalized_key,
                payload_hash,
                payload["rating"],
                payload["outcome"],
                payload["purchase_intent"],
                payload["missing_feature"],
                payload["notes"],
                1 if payload["contact_consent"] else 0,
                submitted_by[:300],
                now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "id": feedback_id,
        "workspace_id": workspace_id,
        "idempotency_key": normalized_key,
        **payload,
        "submitted_by": submitted_by[:300],
        "created_at": now,
    }, True


def _feedback_from_row(row) -> dict:
    item = dict(row)
    item.pop("payload_hash", None)
    item["contact_consent"] = bool(item.get("contact_consent"))
    return item


def _first_evidence_backed_task(cur, *, intent: str | None = None) -> dict:
    action_placeholders = ", ".join("?" for _ in _EVIDENCE_ACTIONS)
    intent_clause = "AND tr.intent = ?" if intent else ""
    params = [*_EVIDENCE_ACTIONS]
    if intent:
        params.append(intent)
    cur.execute(
        f"""
        SELECT tr.id, tr.created_at AS at,
               COUNT(DISTINCT ts.action) AS evidence_source_count
        FROM task_runs tr
        JOIN task_steps ts ON ts.task_run_id = tr.id
        WHERE ts.action IN ({action_placeholders})
          AND ts.result_json IS NOT NULL
          AND TRIM(ts.result_json) NOT IN ('', 'null', '{{}}', '[]')
          {intent_clause}
        GROUP BY tr.id, tr.created_at
        ORDER BY tr.created_at ASC, tr.id ASC
        LIMIT 1
        """,
        tuple(params),
    )
    row = cur.fetchone()
    if not row:
        return {"id": None, "at": None, "count": 0, "evidence_source_count": 0}
    item = dict(row)
    item["count"] = 1
    item["evidence_source_count"] = int(item.get("evidence_source_count") or 0)
    return item


def trial_onboarding_status(workspace_id: str) -> dict:
    if workspace_id != configured_workspace_id():
        raise TrialError("unknown workspace")
    workspace = get_workspace(workspace_id)
    if not workspace:
        raise TrialError("unknown workspace")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                MIN(CASE
                    WHEN last_connected_at IS NOT NULL
                    THEN last_connected_at
                END) AS at,
                SUM(CASE
                    WHEN last_connected_at IS NOT NULL
                    THEN 1 ELSE 0
                END) AS count,
                COUNT(*) AS configured_count
            FROM monitored_targets
            """
        )
        target = cur.fetchone()
        first_query = _first_evidence_backed_task(cur)
        first_diagnosis = _first_evidence_backed_task(cur, intent="troubleshoot")
        cur.execute(
            "SELECT MIN(created_at) AS at, COUNT(*) AS count FROM trial_feedback WHERE workspace_id = ?",
            (workspace_id,),
        )
        feedback = cur.fetchone()
        cur.execute(
            "SELECT MIN(created_at) AS at, COUNT(*) AS count FROM pilot_outcomes WHERE workspace_id = ?",
            (workspace_id,),
        )
        value_evidence = cur.fetchone()
    finally:
        conn.close()

    milestones = [
        {
            "id": "trial_activated",
            "label": "激活免费试用",
            "completed": bool(workspace.get("trial_activated_at")),
            "completed_at": workspace.get("trial_activated_at"),
            "next_action": "使用激活令牌领取试用" if not workspace.get("trial_activated_at") else None,
        },
        {
            "id": "target_configured",
            "label": "验证一个真实服务目标",
            "completed": int(target["count"] or 0) > 0,
            "completed_at": target["at"],
            "next_action": (
                "在接入服务中添加健康检查地址"
                if int(target["configured_count"] or 0) == 0
                else "重试服务目标连接验证"
            )
            if int(target["count"] or 0) == 0
            else None,
        },
        {
            "id": "first_query",
            "label": "完成首次状态查询",
            "completed": int(first_query["count"]) > 0,
            "completed_at": first_query["at"],
            "next_action": "询问：服务名 状态" if int(first_query["count"]) == 0 else None,
        },
        {
            "id": "first_diagnosis",
            "label": "完成首次故障诊断",
            "completed": int(first_diagnosis["count"]) > 0,
            "completed_at": first_diagnosis["at"],
            "next_action": "询问：服务名 报警了，帮我排查" if int(first_diagnosis["count"]) == 0 else None,
        },
        {
            "id": "feedback_submitted",
            "label": "提交试用反馈",
            "completed": int(feedback["count"]) > 0,
            "completed_at": feedback["at"],
            "next_action": "提交评分、使用结果和缺失能力" if int(feedback["count"]) == 0 else None,
        },
    ]
    completed = sum(1 for milestone in milestones if milestone["completed"])
    activation_at = _parse_datetime(workspace.get("trial_activated_at"))
    first_value_at = _parse_datetime(first_diagnosis["at"])
    time_to_first_value_minutes = None
    if activation_at and first_value_at and first_value_at >= activation_at:
        time_to_first_value_minutes = round(
            (first_value_at - activation_at).total_seconds() / 60,
            2,
        )
    next_milestone = next(
        (milestone for milestone in milestones if not milestone["completed"]),
        None,
    )
    return {
        "workspace_id": workspace_id,
        "subscription": get_subscription_status(workspace_id),
        "milestones": milestones,
        "completed_milestones": completed,
        "total_milestones": len(milestones),
        "progress_percent": round(completed * 100 / len(milestones)),
        "next_milestone": next_milestone,
        "first_value_at": first_diagnosis["at"],
        "first_value_evidence_sources": int(
            first_diagnosis["evidence_source_count"]
        ),
        "time_to_first_value_minutes": time_to_first_value_minutes,
        "value_evidence_count": int(value_evidence["count"]),
        "feedback_count": int(feedback["count"]),
        "configured_target_count": int(target["configured_count"] or 0),
        "verified_target_count": int(target["count"] or 0),
        "paid_upgrade": {
            "available": bool(_upgrade_contact_url()),
            "contact_url": _upgrade_contact_url(),
            "payment_automation": False,
            "message": "付费套餐和账单能力已预留；当前通过人工联系完成升级。",
        },
    }


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def trial_conversion_metrics(workspace_id: str) -> dict:
    onboarding = trial_onboarding_status(workspace_id)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT COUNT(*) AS count, AVG(rating) AS average_rating,
                   SUM(CASE WHEN purchase_intent = 'yes' THEN 1 ELSE 0 END) AS yes_count,
                   SUM(CASE WHEN purchase_intent = 'maybe' THEN 1 ELSE 0 END) AS maybe_count,
                   SUM(CASE WHEN outcome = 'high_value' THEN 1 ELSE 0 END) AS high_value_count,
                   SUM(CASE WHEN contact_consent = 1 THEN 1 ELSE 0 END) AS contact_consent_count
            FROM trial_feedback WHERE workspace_id = ?
            """,
            (workspace_id,),
        )
        summary = dict(cur.fetchone())
        cur.execute(
            """
            SELECT id, rating, outcome, purchase_intent, missing_feature,
                   notes, contact_consent, submitted_by, created_at
            FROM trial_feedback WHERE workspace_id = ?
            ORDER BY created_at DESC LIMIT 100
            """,
            (workspace_id,),
        )
        feedback = [_feedback_from_row(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT contact_email, admin_name, activated_at, expires_at
            FROM trial_activations WHERE workspace_id = ?
            """,
            (workspace_id,),
        )
        activation_row = cur.fetchone()
    finally:
        conn.close()
    return {
        "workspace_id": workspace_id,
        "activation": dict(activation_row) if activation_row else None,
        "onboarding": onboarding,
        "feedback_summary": {
            "count": int(summary.get("count") or 0),
            "average_rating": round(float(summary["average_rating"]), 2)
            if summary.get("average_rating") is not None
            else None,
            "purchase_intent_yes": int(summary.get("yes_count") or 0),
            "purchase_intent_maybe": int(summary.get("maybe_count") or 0),
            "high_value": int(summary.get("high_value_count") or 0),
            "contact_consent": int(summary.get("contact_consent_count") or 0),
        },
        "feedback": feedback,
    }
