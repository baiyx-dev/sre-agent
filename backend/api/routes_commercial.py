import csv
import io
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.security_auth import Principal, require_admin, require_viewer
from backend.services.commercial_service import (
    PlanEntitlementError,
    get_plan_entitlements,
    get_usage_summary,
    get_workspace,
    issue_workspace_api_key,
    list_usage_events,
    list_workspace_api_keys,
    revoke_workspace_api_key,
)


router = APIRouter(tags=["commercial"])


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="viewer")


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
        }
    }


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
