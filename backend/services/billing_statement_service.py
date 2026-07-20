import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from backend.storage.db import configured_workspace_id, get_conn


class BillingStatementError(ValueError):
    pass


class BillingStatementConflict(BillingStatementError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise BillingStatementError("month must use YYYY-MM format") from exc
    if start.strftime("%Y-%m") != month:
        raise BillingStatementError("month must use YYYY-MM format")
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def _configured_money(name: str) -> Decimal:
    raw = os.getenv(name, "0").strip() or "0"
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise BillingStatementError(f"{name} must be a non-negative USD amount") from exc
    if not value.is_finite() or value < 0:
        raise BillingStatementError(f"{name} must be a non-negative USD amount")
    return value


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _canonical_payload(payload: dict) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _snapshot_components(cur, *, workspace_id: str, month: str) -> dict:
    start, end = _month_bounds(month)
    cur.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
    workspace_row = cur.fetchone()
    if not workspace_row:
        raise BillingStatementError("unknown workspace")
    workspace = dict(workspace_row)
    cur.execute(
        """
        SELECT metric, SUM(quantity) AS quantity
        FROM usage_events
        WHERE workspace_id = ? AND occurred_at >= ? AND occurred_at < ?
        GROUP BY metric ORDER BY metric
        """,
        (workspace_id, start.isoformat(), end.isoformat()),
    )
    usage = {row["metric"]: int(row["quantity"] or 0) for row in cur.fetchall()}
    requests_used = usage.get("api_request", 0)
    included_requests = int(workspace.get("monthly_request_limit") or 0)
    overage_requests = (
        max(0, requests_used - included_requests) if included_requests > 0 else 0
    )
    base_fee = _configured_money("SRE_PLAN_PRICE_USD_MONTHLY")
    overage_rate = _configured_money("SRE_REQUEST_OVERAGE_USD_PER_1000")
    if workspace["plan"] != "trial" and base_fee <= 0:
        raise BillingStatementError(
            "SRE_PLAN_PRICE_USD_MONTHLY must be greater than zero for a paid plan"
        )
    overage_fee = (Decimal(overage_requests) / Decimal(1000)) * overage_rate
    amount_due = base_fee + overage_fee
    llm_cost = Decimal(usage.get("llm_cost_usd_micro", 0)) / Decimal(1_000_000)
    warnings = []
    if overage_requests and overage_rate == 0:
        warnings.append("request overage exists but its configured rate is zero")
    return {
        "workspace": {
            "id": workspace_id,
            "name": workspace["name"],
            "plan": workspace["plan"],
            "subscription_status": workspace.get("subscription_status"),
        },
        "month": month,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "usage": {
            "by_metric": usage,
            "requests_used": requests_used,
            "included_requests": included_requests,
            "overage_requests": overage_requests,
        },
        "pricing": {
            "currency": "USD",
            "base_fee_usd": _money(base_fee),
            "overage_rate_usd_per_1000_requests": _money(overage_rate),
            "overage_fee_usd": _money(overage_fee),
            "amount_due_usd": _money(amount_due),
        },
        "internal_cost": {
            "llm_cost_usd": _money(llm_cost),
        },
        "configuration_warnings": warnings,
    }


def _statement_from_row(row) -> dict | None:
    if not row:
        return None
    item = dict(row)
    raw_payload = item.pop("payload_json")
    try:
        payload = json.loads(raw_payload)
        canonical = _canonical_payload(payload)
        calculated_hash = _payload_hash(canonical)
        parse_error = None
    except (TypeError, json.JSONDecodeError) as exc:
        payload = None
        calculated_hash = None
        parse_error = str(exc)
    item["payload"] = payload
    item["integrity"] = {
        "valid": bool(
            calculated_hash
            and hmac.compare_digest(item["payload_hash"], calculated_hash)
        ),
        "stored_hash": item["payload_hash"],
        "calculated_hash": calculated_hash,
        "parse_error": parse_error,
    }
    return item


def preview_billing_statement(
    *,
    workspace_id: str,
    month: str,
    now: datetime | None = None,
) -> dict:
    if workspace_id != configured_workspace_id():
        raise BillingStatementError("unknown workspace")
    _, end = _month_bounds(month)
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    conn = get_conn()
    cur = conn.cursor()
    try:
        preview = _snapshot_components(cur, workspace_id=workspace_id, month=month)
        cur.execute(
            """
            SELECT * FROM billing_statements
            WHERE workspace_id = ? AND month = ?
            """,
            (workspace_id, month),
        )
        existing = _statement_from_row(cur.fetchone())
    finally:
        conn.close()
    return {
        "preview": preview,
        "period_closed": end <= current,
        "finalizable": end <= current and existing is None,
        "existing_statement": existing,
    }


def finalize_billing_statement(
    *,
    workspace_id: str,
    month: str,
    idempotency_key: str,
    finalized_by: str,
    now: datetime | None = None,
) -> tuple[dict, bool]:
    if workspace_id != configured_workspace_id():
        raise BillingStatementError("unknown workspace")
    _, end = _month_bounds(month)
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if end > current:
        raise BillingStatementError("only a closed UTC month can be finalized")
    normalized_key = idempotency_key.strip()[:120]
    if not normalized_key:
        raise BillingStatementError("idempotency_key is required")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            """
            SELECT * FROM billing_statements
            WHERE workspace_id = ? AND idempotency_key = ?
            """,
            (workspace_id, normalized_key),
        )
        by_key = cur.fetchone()
        if by_key:
            if by_key["month"] != month:
                raise BillingStatementConflict(
                    "idempotency_key was already used for a different billing month"
                )
            statement = _statement_from_row(by_key)
            if not statement["integrity"]["valid"]:
                raise BillingStatementConflict(
                    "existing billing statement failed integrity verification"
                )
            conn.commit()
            return statement, False

        cur.execute(
            """
            SELECT * FROM billing_statements
            WHERE workspace_id = ? AND month = ?
            """,
            (workspace_id, month),
        )
        by_month = cur.fetchone()
        if by_month:
            statement = _statement_from_row(by_month)
            if not statement["integrity"]["valid"]:
                raise BillingStatementConflict(
                    "existing billing statement failed integrity verification"
                )
            conn.commit()
            return statement, False

        statement_id = str(uuid.uuid4())
        payload = {
            "format_version": 1,
            "statement_id": statement_id,
            "finalized_at": current.isoformat(),
            **_snapshot_components(cur, workspace_id=workspace_id, month=month),
        }
        payload_json = _canonical_payload(payload)
        statement_hash = _payload_hash(payload_json)
        cur.execute(
            """
            INSERT OR IGNORE INTO billing_statements (
                id, workspace_id, month, idempotency_key,
                payload_json, payload_hash, finalized_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                statement_id,
                workspace_id,
                month,
                normalized_key,
                payload_json,
                statement_hash,
                finalized_by[:300],
                current.isoformat(),
            ),
        )
        created = cur.rowcount == 1
        cur.execute(
            """
            SELECT * FROM billing_statements
            WHERE workspace_id = ? AND month = ?
            """,
            (workspace_id, month),
        )
        stored = cur.fetchone()
        if not stored:
            cur.execute(
                """
                SELECT * FROM billing_statements
                WHERE workspace_id = ? AND idempotency_key = ?
                """,
                (workspace_id, normalized_key),
            )
            conflicting = cur.fetchone()
            if conflicting:
                raise BillingStatementConflict(
                    "idempotency_key was already used for a different billing month"
                )
            raise BillingStatementConflict("billing month was finalized concurrently")
        conn.commit()
        return _statement_from_row(stored), created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_billing_statement(*, workspace_id: str, month: str) -> dict | None:
    if workspace_id != configured_workspace_id():
        raise BillingStatementError("unknown workspace")
    _month_bounds(month)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM billing_statements
        WHERE workspace_id = ? AND month = ?
        """,
        (workspace_id, month),
    )
    statement = _statement_from_row(cur.fetchone())
    conn.close()
    return statement


def list_billing_statements(*, workspace_id: str, limit: int = 100) -> dict:
    if workspace_id != configured_workspace_id():
        raise BillingStatementError("unknown workspace")
    safe_limit = max(1, min(int(limit), 1000))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM billing_statements
        WHERE workspace_id = ?
        ORDER BY month DESC
        LIMIT ?
        """,
        (workspace_id, safe_limit),
    )
    statements = [_statement_from_row(row) for row in cur.fetchall()]
    conn.close()
    return {
        "workspace_id": workspace_id,
        "statement_count": len(statements),
        "statements": statements,
    }
