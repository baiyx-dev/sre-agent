from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.security_auth import Principal, principal_subject, require_admin, require_viewer
from backend.services.trial_service import (
    PURCHASE_INTENTS,
    TRIAL_OUTCOMES,
    TrialActivationConflict,
    TrialActivationRateLimited,
    TrialActivationUnauthorized,
    TrialConfigurationError,
    TrialError,
    TrialFeedbackConflict,
    activate_trial,
    public_trial_status,
    record_trial_feedback,
    trial_conversion_metrics,
    trial_onboarding_status,
)


router = APIRouter(prefix="/trial", tags=["trial"])


class TrialActivateRequest(BaseModel):
    activation_token: str = Field(min_length=1, max_length=500)
    workspace_name: str = Field(min_length=1, max_length=100)
    admin_name: str = Field(min_length=1, max_length=100)
    contact_email: str = Field(min_length=3, max_length=254)


class TrialFeedbackRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    rating: int = Field(ge=1, le=5)
    outcome: str = Field(default="not_evaluated")
    purchase_intent: str = Field(default="maybe")
    missing_feature: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=2000)
    contact_consent: bool = False


@router.get("/status")
def trial_status():
    try:
        return public_trial_status()
    except TrialError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/activate", status_code=201)
def trial_activate(req: TrialActivateRequest, request: Request):
    requester = request.client.host if request.client else "unknown"
    try:
        return activate_trial(
            activation_token=req.activation_token,
            workspace_name=req.workspace_name,
            admin_name=req.admin_name,
            contact_email=req.contact_email,
            requester=requester,
        )
    except TrialActivationUnauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except TrialActivationRateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "900"},
        ) from exc
    except TrialActivationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TrialConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TrialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/onboarding")
def trial_onboarding(principal: Principal = Depends(require_viewer)):
    try:
        return trial_onboarding_status(principal.workspace_id)
    except TrialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/feedback", status_code=201)
def trial_feedback(
    req: TrialFeedbackRequest,
    principal: Principal = Depends(require_viewer),
):
    if req.outcome not in TRIAL_OUTCOMES:
        raise HTTPException(
            status_code=400,
            detail=f"outcome must be one of: {', '.join(sorted(TRIAL_OUTCOMES))}",
        )
    if req.purchase_intent not in PURCHASE_INTENTS:
        raise HTTPException(
            status_code=400,
            detail=f"purchase_intent must be one of: {', '.join(sorted(PURCHASE_INTENTS))}",
        )
    try:
        feedback, created = record_trial_feedback(
            workspace_id=principal.workspace_id,
            idempotency_key=req.idempotency_key,
            rating=req.rating,
            outcome=req.outcome,
            purchase_intent=req.purchase_intent,
            missing_feature=req.missing_feature,
            notes=req.notes,
            contact_consent=req.contact_consent,
            submitted_by=principal_subject(principal),
        )
        return {"created": created, "feedback": feedback}
    except TrialFeedbackConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TrialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/conversion-metrics")
def trial_metrics(principal: Principal = Depends(require_admin)):
    try:
        return trial_conversion_metrics(principal.workspace_id)
    except TrialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
