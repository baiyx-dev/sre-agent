import hashlib
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from backend.storage.db import configured_workspace_id, get_conn


VALID_OUTCOME_CATEGORIES = {"diagnosis", "incident", "change", "support", "other"}
_MAX_REPORT_DAYS = 366
_MONTH_DAYS = Decimal("30.4375")


class PilotOutcomeError(ValueError):
    pass


class PilotOutcomeConflict(PilotOutcomeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str, *, field_name: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PilotOutcomeError(f"{field_name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def selected_period_bounds(
    *,
    month: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[datetime, datetime]:
    if month and (start_date or end_date):
        raise PilotOutcomeError("month cannot be combined with start_date or end_date")
    if bool(start_date) != bool(end_date):
        raise PilotOutcomeError("start_date and end_date must be provided together")
    if month:
        try:
            start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise PilotOutcomeError("month must use YYYY-MM format") from exc
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    elif start_date and end_date:
        try:
            start_day = date.fromisoformat(start_date)
            end_day = date.fromisoformat(end_date)
        except ValueError as exc:
            raise PilotOutcomeError("start_date and end_date must use YYYY-MM-DD format") from exc
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    else:
        now = _utc_now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    days = (end - start).total_seconds() / 86400
    if days <= 0:
        raise PilotOutcomeError("end_date must not be earlier than start_date")
    if days > _MAX_REPORT_DAYS:
        raise PilotOutcomeError(f"report period cannot exceed {_MAX_REPORT_DAYS} days")
    return start, end


def _normalized_optional_minutes(value: int | None, field_name: str) -> int | None:
    if value is None:
        return None
    normalized = int(value)
    if normalized < 0 or normalized > 1_000_000:
        raise PilotOutcomeError(f"{field_name} must be between 0 and 1000000")
    return normalized


def _normalized_bool(value: bool | None) -> int | None:
    return None if value is None else int(bool(value))


def _outcome_from_row(row) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item.pop("payload_hash", None)
    for key in ("recommendation_accepted", "successful"):
        if item.get(key) is not None:
            item[key] = bool(item[key])
    return item


def record_pilot_outcome(
    *,
    workspace_id: str,
    idempotency_key: str,
    category: str,
    recorded_by: str,
    incident_id: str | None = None,
    change_request_id: str | None = None,
    service_name: str | None = None,
    baseline_minutes: int | None = None,
    actual_minutes: int | None = None,
    support_minutes: int = 0,
    recommendation_accepted: bool | None = None,
    successful: bool | None = None,
    notes: str | None = None,
    occurred_at: str | None = None,
) -> tuple[dict, bool]:
    if workspace_id != configured_workspace_id():
        raise PilotOutcomeError("unknown workspace")
    normalized_key = idempotency_key.strip()[:120]
    if not normalized_key:
        raise PilotOutcomeError("idempotency_key is required")
    normalized_category = category.strip().lower()
    if normalized_category not in VALID_OUTCOME_CATEGORIES:
        raise PilotOutcomeError(
            "category must be diagnosis, incident, change, support, or other"
        )
    normalized_baseline = _normalized_optional_minutes(baseline_minutes, "baseline_minutes")
    normalized_actual = _normalized_optional_minutes(actual_minutes, "actual_minutes")
    normalized_support = _normalized_optional_minutes(support_minutes, "support_minutes") or 0
    normalized_occurred_at = (
        _parse_datetime(occurred_at, field_name="occurred_at") if occurred_at else _utc_now()
    ).isoformat()
    payload = {
        "category": normalized_category,
        "incident_id": (incident_id or "").strip()[:100] or None,
        "change_request_id": (change_request_id or "").strip()[:100] or None,
        "service_name": (service_name or "").strip()[:200] or None,
        "baseline_minutes": normalized_baseline,
        "actual_minutes": normalized_actual,
        "support_minutes": normalized_support,
        "recommendation_accepted": recommendation_accepted,
        "successful": successful,
        "notes": (notes or "").strip()[:2000] or None,
        "occurred_at": normalized_occurred_at,
    }
    payload_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    outcome_id = str(uuid.uuid4())
    created_at = _utc_now().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        if payload["incident_id"]:
            cur.execute("SELECT id FROM incidents WHERE id = ?", (payload["incident_id"],))
            if not cur.fetchone():
                raise PilotOutcomeError("incident_id does not exist")
        if payload["change_request_id"]:
            cur.execute(
                "SELECT id FROM change_requests WHERE id = ?",
                (payload["change_request_id"],),
            )
            if not cur.fetchone():
                raise PilotOutcomeError("change_request_id does not exist")
        cur.execute(
            """
            INSERT OR IGNORE INTO pilot_outcomes (
                id, workspace_id, idempotency_key, payload_hash, category,
                incident_id, change_request_id, service_name,
                baseline_minutes, actual_minutes, support_minutes,
                recommendation_accepted, successful, notes, recorded_by,
                occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome_id,
                workspace_id,
                normalized_key,
                payload_hash,
                payload["category"],
                payload["incident_id"],
                payload["change_request_id"],
                payload["service_name"],
                payload["baseline_minutes"],
                payload["actual_minutes"],
                payload["support_minutes"],
                _normalized_bool(payload["recommendation_accepted"]),
                _normalized_bool(payload["successful"]),
                payload["notes"],
                recorded_by[:300],
                payload["occurred_at"],
                created_at,
            ),
        )
        created = cur.rowcount == 1
        cur.execute(
            """
            SELECT * FROM pilot_outcomes
            WHERE workspace_id = ? AND idempotency_key = ?
            """,
            (workspace_id, normalized_key),
        )
        row = cur.fetchone()
        if not row or row["payload_hash"] != payload_hash:
            raise PilotOutcomeConflict(
                "idempotency_key was already used with a different outcome payload"
            )
        conn.commit()
        return _outcome_from_row(row), created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_pilot_outcomes(
    *,
    workspace_id: str,
    month: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50_000,
) -> dict:
    if workspace_id != configured_workspace_id():
        raise PilotOutcomeError("unknown workspace")
    start, end = selected_period_bounds(
        month=month,
        start_date=start_date,
        end_date=end_date,
    )
    safe_limit = max(1, min(int(limit), 50_000))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM pilot_outcomes
        WHERE workspace_id = ? AND occurred_at >= ? AND occurred_at < ?
        ORDER BY occurred_at ASC, id ASC
        LIMIT ?
        """,
        (workspace_id, start.isoformat(), end.isoformat(), safe_limit),
    )
    rows = [_outcome_from_row(row) for row in cur.fetchall()]
    conn.close()
    return {
        "workspace_id": workspace_id,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "outcome_count": len(rows),
        "truncated": len(rows) == safe_limit,
        "outcomes": rows,
    }


def _duration_minutes(start_value: str | None, end_value: str | None) -> float | None:
    if not start_value or not end_value:
        return None
    try:
        start = _parse_datetime(start_value, field_name="start")
        end = _parse_datetime(end_value, field_name="end")
    except PilotOutcomeError:
        return None
    return max(0.0, (end - start).total_seconds() / 60)


def _percentile(values: list[float], percentage: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) * percentage) + 0.999999) - 1))
    return ordered[index]


def _money_env(name: str) -> Decimal:
    raw = os.getenv(name, "0").strip() or "0"
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return Decimal("0")
    return max(Decimal("0"), value)


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def build_pilot_value_report(
    *,
    workspace_id: str,
    month: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    if workspace_id != configured_workspace_id():
        raise PilotOutcomeError("unknown workspace")
    start, end = selected_period_bounds(
        month=month,
        start_date=start_date,
        end_date=end_date,
    )
    start_value = start.isoformat()
    end_value = end.isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT plan FROM workspaces WHERE id = ?", (workspace_id,))
    workspace = cur.fetchone()
    if not workspace:
        conn.close()
        raise PilotOutcomeError("unknown workspace")

    cur.execute("SELECT COUNT(*) AS count FROM services")
    registered_services = int(cur.fetchone()["count"])
    cur.execute(
        """
        SELECT COUNT(DISTINCT id) AS count FROM workspace_api_keys
        WHERE workspace_id = ? AND last_used_at >= ? AND last_used_at < ?
        """,
        (workspace_id, start_value, end_value),
    )
    active_api_keys = int(cur.fetchone()["count"])
    cur.execute(
        """
        SELECT status, COUNT(*) AS count FROM incidents
        WHERE created_at >= ? AND created_at < ? GROUP BY status
        """,
        (start_value, end_value),
    )
    incidents_by_status = {row["status"]: int(row["count"]) for row in cur.fetchall()}
    cur.execute(
        """
        SELECT first_seen_at, resolved_at FROM incidents
        WHERE resolved_at >= ? AND resolved_at < ?
        """,
        (start_value, end_value),
    )
    resolution_minutes = [
        duration
        for row in cur.fetchall()
        if (duration := _duration_minutes(row["first_seen_at"], row["resolved_at"])) is not None
    ]
    cur.execute(
        """
        SELECT status, COUNT(*) AS count FROM change_requests
        WHERE created_at >= ? AND created_at < ? GROUP BY status
        """,
        (start_value, end_value),
    )
    changes_by_status = {row["status"]: int(row["count"]) for row in cur.fetchall()}
    cur.execute(
        """
        SELECT metric, SUM(quantity) AS quantity FROM usage_events
        WHERE workspace_id = ? AND occurred_at >= ? AND occurred_at < ?
        GROUP BY metric ORDER BY metric
        """,
        (workspace_id, start_value, end_value),
    )
    usage = {row["metric"]: int(row["quantity"] or 0) for row in cur.fetchall()}
    cur.execute(
        """
        SELECT * FROM pilot_outcomes
        WHERE workspace_id = ? AND occurred_at >= ? AND occurred_at < ?
        ORDER BY occurred_at ASC, id ASC
        """,
        (workspace_id, start_value, end_value),
    )
    outcomes = [_outcome_from_row(row) for row in cur.fetchall()]
    conn.close()

    measured = [
        item
        for item in outcomes
        if item["baseline_minutes"] is not None and item["actual_minutes"] is not None
    ]
    baseline_minutes = sum(item["baseline_minutes"] for item in measured)
    actual_minutes = sum(item["actual_minutes"] for item in measured)
    net_minutes_saved = baseline_minutes - actual_minutes
    recommendation_outcomes = [
        item for item in outcomes if item["recommendation_accepted"] is not None
    ]
    accepted = sum(1 for item in recommendation_outcomes if item["recommendation_accepted"])
    success_outcomes = [item for item in outcomes if item["successful"] is not None]
    successful = sum(1 for item in success_outcomes if item["successful"])
    support_minutes = sum(int(item["support_minutes"] or 0) for item in outcomes)

    period_days = Decimal(str((end - start).total_seconds() / 86400))
    proration = period_days / _MONTH_DAYS
    monthly_price = _money_env("SRE_PLAN_PRICE_USD_MONTHLY")
    monthly_infra = _money_env("SRE_INFRA_COST_USD_MONTHLY")
    customer_hourly = _money_env("SRE_CUSTOMER_HOURLY_COST_USD")
    support_hourly = _money_env("SRE_SUPPORT_HOURLY_COST_USD")
    recognized_revenue = monthly_price * proration
    infrastructure_cost = monthly_infra * proration
    llm_cost = Decimal(usage.get("llm_cost_usd_micro", 0)) / Decimal(1_000_000)
    support_cost = (Decimal(support_minutes) / Decimal(60)) * support_hourly
    delivery_cost = infrastructure_cost + llm_cost + support_cost
    gross_margin = recognized_revenue - delivery_cost
    labor_value = (Decimal(net_minutes_saved) / Decimal(60)) * customer_hourly
    customer_net_value = labor_value - recognized_revenue

    incident_total = sum(incidents_by_status.values())
    change_total = sum(changes_by_status.values())
    return {
        "workspace_id": workspace_id,
        "plan": workspace["plan"],
        "period_start": start_value,
        "period_end": end_value,
        "period_days": float(period_days),
        "activity": {
            "registered_services": registered_services,
            "active_api_keys": active_api_keys,
            "api_requests": usage.get("api_request", 0),
            "chat_requests": usage.get("chat_request", 0),
        },
        "incidents": {
            "created": incident_total,
            "by_status": incidents_by_status,
            "resolved_in_period": len(resolution_minutes),
            "mttr_minutes": {
                "median": round(_percentile(resolution_minutes, 0.5), 2) if resolution_minutes else None,
                "p95": round(_percentile(resolution_minutes, 0.95), 2) if resolution_minutes else None,
                "average": round(sum(resolution_minutes) / len(resolution_minutes), 2) if resolution_minutes else None,
            },
        },
        "changes": {
            "requested": change_total,
            "by_status": changes_by_status,
            "successful": changes_by_status.get("executed", 0) + changes_by_status.get("dry_run", 0),
            "failed": changes_by_status.get("failed", 0) + changes_by_status.get("unknown", 0),
        },
        "outcomes": {
            "recorded": len(outcomes),
            "measured_time_savings": len(measured),
            "baseline_minutes": baseline_minutes,
            "actual_minutes": actual_minutes,
            "net_minutes_saved": net_minutes_saved,
            "recommendations_measured": len(recommendation_outcomes),
            "recommendations_accepted": accepted,
            "recommendation_acceptance_pct": round(accepted * 100 / len(recommendation_outcomes), 2) if recommendation_outcomes else None,
            "successes_measured": len(success_outcomes),
            "successful_outcomes": successful,
            "success_rate_pct": round(successful * 100 / len(success_outcomes), 2) if success_outcomes else None,
            "support_minutes": support_minutes,
        },
        "usage": {
            "by_metric": usage,
            "llm_cost_usd": _money(llm_cost),
        },
        "economics": {
            "assumptions": {
                "plan_price_usd_monthly": _money(monthly_price),
                "infrastructure_cost_usd_monthly": _money(monthly_infra),
                "customer_hourly_cost_usd": _money(customer_hourly),
                "support_hourly_cost_usd": _money(support_hourly),
            },
            "recognized_revenue_usd": _money(recognized_revenue),
            "customer_labor_value_usd": _money(labor_value),
            "customer_net_value_usd": _money(customer_net_value),
            "infrastructure_cost_usd": _money(infrastructure_cost),
            "support_cost_usd": _money(support_cost),
            "llm_cost_usd": _money(llm_cost),
            "total_delivery_cost_usd": _money(delivery_cost),
            "gross_margin_usd": _money(gross_margin),
            "gross_margin_pct": round(float(gross_margin * 100 / recognized_revenue), 2) if recognized_revenue else None,
            "customer_value_to_revenue_ratio": round(float(labor_value / recognized_revenue), 4) if recognized_revenue else None,
        },
        "evidence_quality": {
            "has_usage": bool(usage),
            "has_resolved_incidents": bool(resolution_minutes),
            "has_time_savings": bool(measured),
            "has_recommendation_feedback": bool(recommendation_outcomes),
            "has_cost_assumptions": all(
                value > 0 for value in (monthly_price, monthly_infra, customer_hourly, support_hourly)
            ),
        },
    }
