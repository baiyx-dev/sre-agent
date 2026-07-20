import csv
import io
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.security_auth import Principal, principal_subject, require_admin, require_viewer
from backend.services.billing_statement_service import (
    BillingStatementConflict,
    BillingStatementError,
    finalize_billing_statement,
    get_billing_statement,
    list_billing_statements,
    preview_billing_statement,
)
from backend.services.commercial_service import (
    PlanEntitlementError,
    get_plan_entitlements,
    get_subscription_status,
    get_usage_summary,
    get_workspace,
    issue_workspace_api_key,
    list_usage_events,
    list_subscription_events,
    list_workspace_api_keys,
    revoke_workspace_api_key,
)
from backend.services.value_report_service import (
    PilotOutcomeConflict,
    PilotOutcomeError,
    build_pilot_value_report,
    list_pilot_outcomes,
    record_pilot_outcome,
)


router = APIRouter(tags=["commercial"])


def _csv_optional(value):
    return "" if value is None else value


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="viewer")


class PilotOutcomeCreateRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=30)
    incident_id: str | None = Field(default=None, max_length=100)
    change_request_id: str | None = Field(default=None, max_length=100)
    service_name: str | None = Field(default=None, max_length=200)
    baseline_minutes: int | None = Field(default=None, ge=0, le=1_000_000)
    actual_minutes: int | None = Field(default=None, ge=0, le=1_000_000)
    support_minutes: int = Field(default=0, ge=0, le=1_000_000)
    recommendation_accepted: bool | None = None
    successful: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)
    occurred_at: str | None = Field(default=None, max_length=50)


class BillingStatementFinalizeRequest(BaseModel):
    month: str = Field(min_length=7, max_length=7)
    idempotency_key: str = Field(min_length=1, max_length=120)


@router.get("/workspace")
def workspace_detail(principal: Principal = Depends(require_viewer)):
    workspace = get_workspace(principal.workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="workspace not found")
    return {
        "workspace": {
            "id": workspace["id"],
            "name": workspace["name"],
            "plan": workspace["plan"],
            "status": workspace["status"],
            "monthly_request_limit": workspace["monthly_request_limit"],
            "entitlements": get_plan_entitlements(workspace["plan"]),
            "subscription": get_subscription_status(principal.workspace_id),
        }
    }


@router.get("/billing/subscription")
def billing_subscription(principal: Principal = Depends(require_viewer)):
    try:
        return {
            "subscription": get_subscription_status(principal.workspace_id),
            "events": list_subscription_events(principal.workspace_id, limit=100),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspace/api-keys")
def api_key_list(principal: Principal = Depends(require_admin)):
    return {"api_keys": list_workspace_api_keys(principal.workspace_id)}


@router.post("/workspace/api-keys", status_code=201)
def api_key_create(req: ApiKeyCreateRequest, principal: Principal = Depends(require_admin)):
    try:
        issued = issue_workspace_api_key(
            req.name,
            req.role,
            workspace_id=principal.workspace_id,
        )
    except PlanEntitlementError as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "plan_entitlement_required",
                "plan": exc.plan,
                "feature": exc.feature,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "api_key": issued,
        "warning": "Copy api_key now; only its prefix is stored and it cannot be recovered.",
    }


@router.delete("/workspace/api-keys/{key_id}")
def api_key_revoke(key_id: str, principal: Principal = Depends(require_admin)):
    try:
        revoked = revoke_workspace_api_key(key_id, principal.workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not revoked:
        raise HTTPException(status_code=404, detail="active API key not found")
    return {"ok": True, "revoked_key_id": key_id}


@router.get("/billing/usage")
def billing_usage(month: str | None = None, principal: Principal = Depends(require_viewer)):
    try:
        return get_usage_summary(principal.workspace_id, month=month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/billing/usage.csv")
def billing_usage_csv(month: str | None = None, principal: Principal = Depends(require_admin)):
    try:
        export = list_usage_events(principal.workspace_id, month=month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "occurred_at",
            "workspace_id",
            "metric",
            "quantity",
            "route",
            "status_code",
            "request_id",
            "metadata_json",
        ]
    )
    for event in export["events"]:
        writer.writerow(
            [
                event["occurred_at"],
                event["workspace_id"],
                event["metric"],
                event["quantity"],
                event.get("route") or "",
                event.get("status_code") or "",
                event.get("request_id") or "",
                json.dumps(event.get("metadata") or {}, ensure_ascii=False, separators=(",", ":")),
            ]
        )
    safe_workspace = re.sub(r"[^a-zA-Z0-9_-]", "-", export["workspace_id"])
    period = export["period_start"][:7]
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sre-usage-{safe_workspace}-{period}.csv"',
            "X-Usage-Event-Count": str(export["event_count"]),
            "X-Usage-Export-Truncated": str(export["truncated"]).lower(),
        },
    )


@router.get("/billing/statements/preview")
def billing_statement_preview(
    month: str,
    principal: Principal = Depends(require_admin),
):
    try:
        return preview_billing_statement(
            workspace_id=principal.workspace_id,
            month=month,
        )
    except BillingStatementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/billing/statements/finalize", status_code=201)
def billing_statement_finalize(
    req: BillingStatementFinalizeRequest,
    principal: Principal = Depends(require_admin),
):
    try:
        statement, created = finalize_billing_statement(
            workspace_id=principal.workspace_id,
            month=req.month,
            idempotency_key=req.idempotency_key,
            finalized_by=principal_subject(principal),
        )
    except BillingStatementConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BillingStatementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "statement": statement,
        "created": created,
        "idempotent_replay": not created,
    }


@router.get("/billing/statements")
def billing_statement_list(
    limit: int = 100,
    principal: Principal = Depends(require_admin),
):
    try:
        return list_billing_statements(
            workspace_id=principal.workspace_id,
            limit=limit,
        )
    except BillingStatementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/billing/statements/{month}")
def billing_statement_detail(
    month: str,
    principal: Principal = Depends(require_admin),
):
    try:
        statement = get_billing_statement(
            workspace_id=principal.workspace_id,
            month=month,
        )
    except BillingStatementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not statement:
        raise HTTPException(status_code=404, detail="billing statement not found")
    return {"statement": statement}


@router.get("/billing/statements/{month}/verify")
def billing_statement_verify(
    month: str,
    principal: Principal = Depends(require_admin),
):
    result = billing_statement_detail(month, principal)
    statement = result["statement"]
    return {
        "workspace_id": principal.workspace_id,
        "month": month,
        "statement_id": statement["id"],
        **statement["integrity"],
    }


@router.get("/billing/statements/{month}/export.csv")
def billing_statement_export_csv(
    month: str,
    principal: Principal = Depends(require_admin),
):
    result = billing_statement_detail(month, principal)
    statement = result["statement"]
    if not statement["integrity"]["valid"]:
        raise HTTPException(
            status_code=409,
            detail="billing statement failed integrity verification",
        )
    payload = statement.get("payload") or {}
    workspace = payload.get("workspace") or {}
    usage = payload.get("usage") or {}
    pricing = payload.get("pricing") or {}
    internal_cost = payload.get("internal_cost") or {}
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    columns = [
        "statement_id",
        "workspace_id",
        "workspace_name",
        "month",
        "period_start",
        "period_end",
        "plan",
        "subscription_status",
        "requests_used",
        "included_requests",
        "overage_requests",
        "base_fee_usd",
        "overage_rate_usd_per_1000_requests",
        "overage_fee_usd",
        "amount_due_usd",
        "llm_cost_usd",
        "payload_hash",
        "integrity_valid",
        "finalized_at",
        "finalized_by",
        "usage_json",
    ]
    writer.writerow(columns)
    writer.writerow(
        [
            statement["id"],
            statement["workspace_id"],
            workspace.get("name") or "",
            statement["month"],
            payload.get("period_start") or "",
            payload.get("period_end") or "",
            workspace.get("plan") or "",
            workspace.get("subscription_status") or "",
            usage.get("requests_used", 0),
            usage.get("included_requests", 0),
            usage.get("overage_requests", 0),
            pricing.get("base_fee_usd", 0),
            pricing.get("overage_rate_usd_per_1000_requests", 0),
            pricing.get("overage_fee_usd", 0),
            pricing.get("amount_due_usd", 0),
            internal_cost.get("llm_cost_usd", 0),
            statement["payload_hash"],
            str(statement["integrity"]["valid"]).lower(),
            payload.get("finalized_at") or statement["created_at"],
            statement["finalized_by"],
            json.dumps(usage.get("by_metric") or {}, ensure_ascii=False, separators=(",", ":")),
        ]
    )
    safe_workspace = re.sub(r"[^a-zA-Z0-9_-]", "-", statement["workspace_id"])
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="sre-statement-{safe_workspace}-{month}.csv"'
            ),
            "X-Billing-Statement-Hash": statement["payload_hash"],
            "X-Billing-Statement-Integrity": str(
                statement["integrity"]["valid"]
            ).lower(),
        },
    )
@router.post("/billing/pilot-outcomes", status_code=201)
def pilot_outcome_create(
    req: PilotOutcomeCreateRequest,
    principal: Principal = Depends(require_admin),
):
    try:
        outcome, created = record_pilot_outcome(
            workspace_id=principal.workspace_id,
            recorded_by=principal_subject(principal),
            **req.model_dump(),
        )
    except PilotOutcomeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PilotOutcomeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"outcome": outcome, "created": created, "idempotent_replay": not created}


@router.get("/billing/pilot-outcomes")
def pilot_outcome_list(
    month: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50_000,
    principal: Principal = Depends(require_admin),
):
    try:
        return list_pilot_outcomes(
            workspace_id=principal.workspace_id,
            month=month,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    except PilotOutcomeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/billing/value-report")
def billing_value_report(
    month: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    principal: Principal = Depends(require_admin),
):
    try:
        return build_pilot_value_report(
            workspace_id=principal.workspace_id,
            month=month,
            start_date=start_date,
            end_date=end_date,
        )
    except PilotOutcomeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/billing/value-report.csv")
def billing_value_report_csv(
    month: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    principal: Principal = Depends(require_admin),
):
    try:
        report = build_pilot_value_report(
            workspace_id=principal.workspace_id,
            month=month,
            start_date=start_date,
            end_date=end_date,
        )
    except PilotOutcomeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    activity = report["activity"]
    incidents = report["incidents"]
    changes = report["changes"]
    outcomes = report["outcomes"]
    economics = report["economics"]
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    columns = [
        "workspace_id",
        "plan",
        "period_start",
        "period_end",
        "registered_services",
        "active_api_keys",
        "api_requests",
        "incidents_created",
        "incidents_resolved",
        "mttr_median_minutes",
        "mttr_p95_minutes",
        "changes_requested",
        "changes_successful",
        "changes_failed",
        "outcomes_recorded",
        "net_minutes_saved",
        "recommendation_acceptance_pct",
        "outcome_success_rate_pct",
        "support_minutes",
        "llm_cost_usd",
        "recognized_revenue_usd",
        "customer_labor_value_usd",
        "total_delivery_cost_usd",
        "gross_margin_usd",
        "gross_margin_pct",
    ]
    writer.writerow(columns)
    writer.writerow(
        [
            report["workspace_id"],
            report["plan"],
            report["period_start"],
            report["period_end"],
            activity["registered_services"],
            activity["active_api_keys"],
            activity["api_requests"],
            incidents["created"],
            incidents["resolved_in_period"],
            _csv_optional(incidents["mttr_minutes"]["median"]),
            _csv_optional(incidents["mttr_minutes"]["p95"]),
            changes["requested"],
            changes["successful"],
            changes["failed"],
            outcomes["recorded"],
            outcomes["net_minutes_saved"],
            _csv_optional(outcomes["recommendation_acceptance_pct"]),
            _csv_optional(outcomes["success_rate_pct"]),
            outcomes["support_minutes"],
            report["usage"]["llm_cost_usd"],
            economics["recognized_revenue_usd"],
            economics["customer_labor_value_usd"],
            economics["total_delivery_cost_usd"],
            economics["gross_margin_usd"],
            _csv_optional(economics["gross_margin_pct"]),
        ]
    )
    safe_workspace = re.sub(r"[^a-zA-Z0-9_-]", "-", report["workspace_id"])
    period = report["period_start"][:10]
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sre-value-{safe_workspace}-{period}.csv"',
            "X-Pilot-Outcome-Count": str(outcomes["recorded"]),
        },
    )
