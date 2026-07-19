from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from backend.security_auth import principal_subject, require_admin, require_operator, require_viewer
from backend.services.change_request_service import (
    ChangeRequestServiceError,
    confirm_change_request,
    redrive_change_request,
)
from backend.storage.repositories import get_change_request, list_change_requests
from backend.storage.repositories import (
    cancel_change_request,
    get_change_job,
    save_execution_audit,
)

router = APIRouter(
    prefix="/changes",
    tags=["changes"],
    dependencies=[Depends(require_viewer)],
)


class ChangeConfirmRequest(BaseModel):
    dry_run: bool = False


class ChangeCancelRequest(BaseModel):
    reason: str = "cancelled_by_operator"


class ChangeRedriveRequest(BaseModel):
    reason: str


@router.get("")
def change_request_list(status: str | None = None, limit: int = 100):
    return {"change_requests": list_change_requests(status=status, limit=limit)}


@router.get("/{change_request_id}")
def change_request_detail(change_request_id: str):
    change_request = get_change_request(change_request_id)
    if not change_request:
        raise HTTPException(status_code=404, detail="change request not found")
    return {
        "change_request": change_request,
        "job": get_change_job(change_request_id),
    }


@router.post("/{change_request_id}/confirm")
def confirm_change(
    change_request_id: str,
    req: ChangeConfirmRequest,
    x_guard_token: str | None = Header(default=None, alias="X-Guard-Token"),
    _principal=Depends(require_operator),
):
    try:
        result, _ = confirm_change_request(
            change_request_id=change_request_id,
            dry_run=req.dry_run,
            guard_token=x_guard_token,
            source="changes_api",
            approved_by=principal_subject(_principal),
        )
    except ChangeRequestServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return result


@router.post("/{change_request_id}/cancel")
def cancel_change(
    change_request_id: str,
    req: ChangeCancelRequest,
    _principal=Depends(require_operator),
):
    actor = principal_subject(_principal)
    reason = req.reason.strip()[:500] or "cancelled_by_operator"
    cancelled, cancel_error = cancel_change_request(
        change_request_id,
        cancelled_by=actor,
        reason=reason,
    )
    if cancel_error:
        status_code = 404 if cancel_error == "change_request_not_found" else 409
        raise HTTPException(status_code=status_code, detail=cancel_error)
    save_execution_audit(
        action=(cancelled or {}).get("action_type", "unknown"),
        service_name=(cancelled or {}).get("service_name"),
        source="changes_api",
        status="cancelled",
        reason=reason,
        actor=actor,
        change_request_id=change_request_id,
    )
    return {"change_request": cancelled}


@router.post("/{change_request_id}/redrive")
def redrive_change(
    change_request_id: str,
    req: ChangeRedriveRequest,
    x_guard_token: str | None = Header(default=None, alias="X-Guard-Token"),
    _principal=Depends(require_admin),
):
    try:
        job, change_request = redrive_change_request(
            change_request_id=change_request_id,
            guard_token=x_guard_token,
            source="changes_api",
            redriven_by=principal_subject(_principal),
            reason=req.reason,
        )
    except ChangeRequestServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"change_request": change_request, "job": job}
