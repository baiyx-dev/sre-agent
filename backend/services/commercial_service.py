import hashlib
import hmac
import json
import math
import secrets
import uuid
from datetime import datetime, timezone

from backend.storage.db import configured_workspace_id, get_conn, is_postgres_database


VALID_ROLES = {"viewer", "operator", "admin"}
VALID_PLANS = {"trial", "starter", "team", "enterprise"}
VALID_SUBSCRIPTION_STATUSES = {
    "pending_activation",
    "trialing",
    "active",
    "past_due",
    "suspended",
    "canceled",
    "expired",
}
PLAN_ENTITLEMENTS = {
    "trial": {"production_writes": False, "max_workspace_api_keys": 3},
    "starter": {"production_writes": False, "max_workspace_api_keys": 10},
    "team": {"production_writes": True, "max_workspace_api_keys": 50},
    "enterprise": {"production_writes": True, "max_workspace_api_keys": 0},
}


class PlanEntitlementError(ValueError):
    def __init__(self, plan: str, feature: str):
        self.plan = plan
        self.feature = feature
        super().__init__(f"plan '{plan}' does not include {feature}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_api_key(value: str) -> str:
    # Keys contain 256 bits of cryptographic randomness. A one-way digest keeps
    # the credential out of the database and still permits indexed lookup.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _month_bounds(now: datetime | None = None) -> tuple[str, str]:
    current = now or _utc_now()
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()


def _selected_month_bounds(month: str | None = None) -> tuple[str, str]:
    if not month:
        return _month_bounds()
    try:
        selected = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("month must use YYYY-MM format") from exc
    return _month_bounds(selected)


def get_workspace(workspace_id: str | None = None) -> dict | None:
    selected_id = workspace_id or configured_workspace_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM workspaces WHERE id = ?", (selected_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_plan_entitlements(plan: str | None = None) -> dict:
    selected_plan = (plan or (get_workspace() or {}).get("plan") or "trial").lower()
    if selected_plan not in PLAN_ENTITLEMENTS:
        selected_plan = "trial"
    return {"plan": selected_plan, **PLAN_ENTITLEMENTS[selected_plan]}


def _parse_utc_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_subscription_status(
    workspace_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    selected_id = workspace_id or configured_workspace_id()
    workspace = get_workspace(selected_id)
    if not workspace or selected_id != configured_workspace_id():
        raise ValueError("unknown workspace")
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    plan = str(workspace.get("plan") or "trial").lower()
    configured_status = str(
        workspace.get("subscription_status")
        or ("trialing" if plan == "trial" else "active")
    ).lower()
    trial_end = _parse_utc_datetime(workspace.get("trial_ends_at"))
    period_end = _parse_utc_datetime(workspace.get("current_period_end"))
    access_allowed = False
    effective_status = configured_status
    blocking_reason = None
    access_end = None

    if workspace.get("status") != "active":
        effective_status = "workspace_suspended"
        blocking_reason = "workspace is not active"
    elif configured_status not in VALID_SUBSCRIPTION_STATUSES:
        effective_status = "configuration_error"
        blocking_reason = "subscription status is invalid"
    elif plan == "trial":
        access_end = trial_end
        if configured_status == "pending_activation":
            effective_status = "pending_activation"
            access_end = None
            blocking_reason = "trial has not been activated"
        elif configured_status in {"suspended", "canceled", "expired"}:
            blocking_reason = f"trial is {configured_status}"
        elif configured_status not in {"trialing", "active"}:
            effective_status = "configuration_error"
            blocking_reason = f"status {configured_status} is invalid for a trial plan"
        elif not trial_end:
            effective_status = "configuration_error"
            blocking_reason = "trial end is not configured"
        elif current >= trial_end:
            effective_status = "expired"
            blocking_reason = "trial has expired"
        else:
            effective_status = "trialing"
            access_allowed = True
    elif configured_status in {"trialing", "expired"}:
        effective_status = "configuration_error"
        blocking_reason = f"status {configured_status} is invalid for a paid plan"
    elif configured_status == "active":
        access_allowed = True
        access_end = period_end
    elif configured_status in {"past_due", "canceled"} and period_end and current < period_end:
        effective_status = "grace_period" if configured_status == "past_due" else "canceling"
        access_allowed = True
        access_end = period_end
    else:
        access_end = period_end
        blocking_reason = f"subscription is {configured_status}"

    seconds_remaining = (
        max(0.0, (access_end - current).total_seconds()) if access_end else None
    )
    return {
        "workspace_id": selected_id,
        "plan": plan,
        "configured_status": configured_status,
        "effective_status": effective_status,
        "access_allowed": access_allowed,
        "upgrade_required": not access_allowed,
        "blocking_reason": blocking_reason,
        "trial_activated_at": workspace.get("trial_activated_at"),
        "trial_ends_at": workspace.get("trial_ends_at"),
        "current_period_end": workspace.get("current_period_end"),
        "access_ends_at": access_end.isoformat() if access_end else None,
        "days_remaining": math.ceil(seconds_remaining / 86400)
        if seconds_remaining is not None
        else None,
        "subscription_updated_at": workspace.get("subscription_updated_at"),
    }


def list_subscription_events(
    workspace_id: str | None = None,
    *,
    limit: int = 100,
) -> list[dict]:
    selected_id = workspace_id or configured_workspace_id()
    if selected_id != configured_workspace_id():
        raise ValueError("unknown workspace")
    safe_limit = max(1, min(int(limit), 1000))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, workspace_id, previous_state_json, new_state_json,
               actor, reason, created_at
        FROM subscription_events
        WHERE workspace_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (selected_id, safe_limit),
    )
    events = []
    for row in cur.fetchall():
        item = dict(row)
        for source, target in (
            ("previous_state_json", "previous_state"),
            ("new_state_json", "new_state"),
        ):
            raw = item.pop(source, None)
            item[target] = json.loads(raw) if raw else None
        events.append(item)
    conn.close()
    return events


def workspace_subscription_access_allowed(workspace_id: str | None = None) -> bool:
    try:
        return bool(get_subscription_status(workspace_id)["access_allowed"])
    except Exception:
        return False


def production_write_entitled() -> bool:
    try:
        return bool(get_plan_entitlements()["production_writes"])
    except Exception:
        return False


def workspace_configuration_status() -> dict:
    try:
        workspace = get_workspace()
    except Exception:
        workspace = None
    try:
        subscription = get_subscription_status((workspace or {}).get("id"))
    except Exception:
        subscription = {
            "effective_status": "unavailable",
            "access_allowed": False,
        }
    return {
        "configured": bool(workspace),
        "workspace_id": (workspace or {}).get("id"),
        "plan": (workspace or {}).get("plan"),
        "active": (workspace or {}).get("status") == "active",
        "monthly_request_limit": (workspace or {}).get("monthly_request_limit"),
        "entitlements": get_plan_entitlements((workspace or {}).get("plan")),
        "subscription": subscription,
    }


def issue_workspace_api_key(
    name: str,
    role: str,
    *,
    workspace_id: str | None = None,
) -> dict:
    selected_id = workspace_id or configured_workspace_id()
    if selected_id != configured_workspace_id():
        raise ValueError("this deployment only manages its configured workspace")
    normalized_name = name.strip()[:100]
    normalized_role = role.strip().lower()
    if not normalized_name:
        raise ValueError("API key name is required")
    if normalized_role not in VALID_ROLES:
        raise ValueError("role must be viewer, operator, or admin")
    workspace = get_workspace(selected_id)
    if not workspace or workspace["status"] != "active":
        raise ValueError("workspace is not active")

    entitlements = get_plan_entitlements(workspace["plan"])
    key_id = str(uuid.uuid4())
    raw_key = f"sre_live_{secrets.token_urlsafe(32)}"
    key_hash = _hash_api_key(raw_key)
    key_prefix = raw_key[:17]
    created_at = _utc_now().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        if is_postgres_database():
            cur.execute("SELECT id FROM workspaces WHERE id = ? FOR UPDATE", (selected_id,))
        cur.execute(
            """
            SELECT COUNT(*) AS count FROM workspace_api_keys
            WHERE workspace_id = ? AND revoked_at IS NULL
            """,
            (selected_id,),
        )
        active_key_count = int(cur.fetchone()["count"])
        max_keys = int(entitlements["max_workspace_api_keys"])
        if max_keys and active_key_count >= max_keys:
            raise PlanEntitlementError(workspace["plan"], "additional workspace API keys")
        cur.execute(
            """
            INSERT INTO workspace_api_keys (
                id, workspace_id, name, role, key_prefix, key_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_id,
                selected_id,
                normalized_name,
                normalized_role,
                key_prefix,
                key_hash,
                created_at,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "id": key_id,
        "workspace_id": selected_id,
        "name": normalized_name,
        "role": normalized_role,
        "key_prefix": key_prefix,
        "api_key": raw_key,
        "created_at": created_at,
    }


def authenticate_workspace_api_key(provided_key: str) -> dict | None:
    if not provided_key.startswith("sre_live_"):
        return None
    candidate_hash = _hash_api_key(provided_key)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT k.id, k.workspace_id, k.name, k.role, k.key_hash,
               w.plan, w.status, w.monthly_request_limit
        FROM workspace_api_keys k
        JOIN workspaces w ON w.id = k.workspace_id
        WHERE k.key_hash = ? AND k.revoked_at IS NULL
        """,
        (candidate_hash,),
    )
    row = cur.fetchone()
    if not row or not hmac.compare_digest(row["key_hash"], candidate_hash):
        conn.close()
        return None
    if row["workspace_id"] != configured_workspace_id() or row["status"] != "active":
        conn.close()
        return None
    cur.execute(
        "UPDATE workspace_api_keys SET last_used_at = ? WHERE id = ?",
        (_utc_now().isoformat(), row["id"]),
    )
    conn.commit()
    result = dict(row)
    conn.close()
    return result


def count_active_workspace_api_keys() -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) AS count FROM workspace_api_keys
        WHERE workspace_id = ? AND revoked_at IS NULL
        """,
        (configured_workspace_id(),),
    )
    row = cur.fetchone()
    conn.close()
    return int(row["count"] if row else 0)


def list_workspace_api_keys(workspace_id: str | None = None) -> list[dict]:
    selected_id = workspace_id or configured_workspace_id()
    if selected_id != configured_workspace_id():
        return []
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, workspace_id, name, role, key_prefix, created_at,
               last_used_at, revoked_at
        FROM workspace_api_keys
        WHERE workspace_id = ?
        ORDER BY created_at DESC
        """,
        (selected_id,),
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def revoke_workspace_api_key(key_id: str, workspace_id: str | None = None) -> bool:
    selected_id = workspace_id or configured_workspace_id()
    if selected_id != configured_workspace_id():
        return False
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role FROM workspace_api_keys
        WHERE id = ? AND workspace_id = ? AND revoked_at IS NULL
        """,
        (key_id, selected_id),
    )
    target = cur.fetchone()
    if not target:
        conn.close()
        return False
    if target["role"] == "admin":
        cur.execute(
            """
            SELECT COUNT(*) AS count FROM workspace_api_keys
            WHERE workspace_id = ? AND role = 'admin' AND revoked_at IS NULL
            """,
            (selected_id,),
        )
        admin_count = int(cur.fetchone()["count"])
        if admin_count <= 1:
            conn.close()
            raise ValueError("cannot revoke the last active workspace admin key")
    cur.execute(
        """
        UPDATE workspace_api_keys SET revoked_at = ?
        WHERE id = ? AND workspace_id = ? AND revoked_at IS NULL
        """,
        (_utc_now().isoformat(), key_id, selected_id),
    )
    revoked = cur.rowcount == 1
    conn.commit()
    conn.close()
    return revoked


def record_usage_event(
    workspace_id: str,
    *,
    metric: str,
    quantity: int = 1,
    route: str | None = None,
    status_code: int | None = None,
    request_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    safe_quantity = max(0, int(quantity))
    if not safe_quantity:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO usage_events (
            id, workspace_id, metric, quantity, route,
            status_code, request_id, metadata_json, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            workspace_id,
            metric.strip()[:80] or "unknown",
            safe_quantity,
            (route or "")[:200] or None,
            status_code,
            request_id,
            json.dumps(metadata or {}, ensure_ascii=False),
            _utc_now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_usage_summary(workspace_id: str | None = None, month: str | None = None) -> dict:
    selected_id = workspace_id or configured_workspace_id()
    if selected_id != configured_workspace_id():
        raise ValueError("unknown workspace")
    workspace = get_workspace(selected_id)
    if not workspace:
        raise ValueError("unknown workspace")
    period_start, period_end = _selected_month_bounds(month)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT metric, SUM(quantity) AS quantity
        FROM usage_events
        WHERE workspace_id = ? AND occurred_at >= ? AND occurred_at < ?
        GROUP BY metric
        ORDER BY metric
        """,
        (selected_id, period_start, period_end),
    )
    by_metric = {row["metric"]: int(row["quantity"] or 0) for row in cur.fetchall()}
    conn.close()
    used = by_metric.get("api_request", 0)
    limit = int(workspace["monthly_request_limit"] or 0)
    cost_micros = by_metric.get("llm_cost_usd_micro", 0)
    return {
        "workspace_id": selected_id,
        "plan": workspace["plan"],
        "status": workspace["status"],
        "period_start": period_start,
        "period_end": period_end,
        "usage": by_metric,
        "llm_cost_usd_micros": cost_micros,
        "llm_cost_usd": round(cost_micros / 1_000_000, 6),
        "monthly_request_limit": limit,
        "requests_used": used,
        "requests_remaining": None if limit == 0 else max(0, limit - used),
        "limit_reached": bool(limit and used >= limit),
        "entitlements": get_plan_entitlements(workspace["plan"]),
        "subscription": get_subscription_status(selected_id),
    }


def workspace_request_limit_reached(workspace_id: str) -> bool:
    return bool(get_usage_summary(workspace_id)["limit_reached"])


def list_usage_events(
    workspace_id: str | None = None,
    *,
    month: str | None = None,
    limit: int = 50_000,
) -> dict:
    selected_id = workspace_id or configured_workspace_id()
    if selected_id != configured_workspace_id():
        raise ValueError("unknown workspace")
    period_start, period_end = _selected_month_bounds(month)
    safe_limit = max(1, min(int(limit), 50_000))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, workspace_id, metric, quantity, route, status_code,
               request_id, metadata_json, occurred_at
        FROM usage_events
        WHERE workspace_id = ? AND occurred_at >= ? AND occurred_at < ?
        ORDER BY occurred_at ASC, id ASC
        LIMIT ?
        """,
        (selected_id, period_start, period_end, safe_limit),
    )
    rows = []
    for row in cur.fetchall():
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
        rows.append(item)
    conn.close()
    return {
        "workspace_id": selected_id,
        "period_start": period_start,
        "period_end": period_end,
        "event_count": len(rows),
        "truncated": len(rows) == safe_limit,
        "events": rows,
    }
