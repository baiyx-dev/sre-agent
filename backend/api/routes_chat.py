from fastapi import APIRouter, Depends, Header, HTTPException

from backend.agents.orchestrator import run_agent
from backend.schemas.chat import ChatRequest, ChatResponse, ConfirmActionRequest
from backend.storage.repositories import (
    get_chat_session_context,
    save_task_run,
    upsert_chat_session_context,
)
from backend.security_auth import principal_subject, require_operator, require_viewer
from backend.services.change_request_service import (
    ChangeRequestServiceError,
    confirm_change_request,
    submit_change_request,
)

router = APIRouter(tags=["chat"], dependencies=[Depends(require_viewer)])


def _normalize_generation_meta(result: dict) -> dict:
    normalized = dict(result)
    normalized.setdefault("generation_source", "fallback_rule")
    normalized.setdefault("llm_provider", "deepseek")
    normalized.setdefault("used_fallback", True)
    normalized.setdefault("fallback_reason", "rule_only")
    normalized.setdefault("policy_decision", None)
    normalized.setdefault("execution_mode", None)
    normalized.setdefault("session_id", None)
    normalized.setdefault("requires_clarification", False)
    normalized.setdefault("clarification_question", None)
    normalized.setdefault("clarification_options", None)
    return normalized


def _log_generation_path(message: str, result: dict):
    message_summary = message.strip().replace("\n", " ")[:60]
    print(
        "[chat_generation]",
        {
            "message": message_summary,
            "intent": result.get("intent"),
            "generation_source": result.get("generation_source"),
            "fallback_reason": result.get("fallback_reason"),
        },
    )


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, _principal=Depends(require_viewer)):
    session_context = get_chat_session_context(req.session_id) if req.session_id else None
    result = run_agent(
        req.message,
        confirm=req.confirm,
        pending_action=req.pending_action,
        session_context=session_context,
    )
    result = _normalize_generation_meta(result)
    result["session_id"] = req.session_id

    pending_action = result.get("pending_action")
    if result.get("requires_confirmation") and pending_action:
        try:
            change_request = submit_change_request(
                action_type=pending_action.get("action_type"),
                service_name=pending_action.get("service_name"),
                target_version=pending_action.get("target_version"),
                policy_decision=pending_action.get("policy_decision") or {},
                resolved_entities=pending_action.get("resolved_entities") or {},
                source="chat",
                requested_by=principal_subject(_principal),
            )
        except ChangeRequestServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        result["pending_action"] = {
            "change_request_id": change_request["change_request_id"],
            "action_type": change_request["action_type"],
            "service_name": change_request["service_name"],
            "target_version": change_request.get("target_version"),
            "policy_decision": change_request.get("policy_decision") or {},
            "expires_at": change_request["expires_at"],
        }

    resolved_entities = result.get("resolved_entities") or {}
    if req.session_id:
        upsert_chat_session_context(
            req.session_id,
            service_name=resolved_entities.get("service_name"),
            intent=result.get("intent"),
            version=resolved_entities.get("version"),
            env=resolved_entities.get("env"),
            namespace=resolved_entities.get("namespace"),
            cluster=resolved_entities.get("cluster"),
            region=resolved_entities.get("region"),
            action_target=resolved_entities.get("action_target"),
            time_window_minutes=resolved_entities.get("time_window_minutes"),
            pending_intent=(result.get("pending_clarification") or {}).get("intent"),
            pending_missing_fields=(result.get("pending_clarification") or {}).get("missing_fields"),
            pending_question=result.get("clarification_question"),
            pending_options=result.get("clarification_options"),
            clear_pending=not result.get("requires_clarification", False),
        )

    _log_generation_path(req.message, result)
    save_task_run(req.message, result)
    return ChatResponse(**result)


@router.post("/chat/confirm", response_model=ChatResponse)
def confirm_action(
    req: ConfirmActionRequest,
    x_guard_token: str | None = Header(default=None, alias="X-Guard-Token"),
    _principal=Depends(require_operator),
):
    change_request_id = req.change_request_id or (req.pending_action or {}).get("change_request_id")
    if not change_request_id:
        raise HTTPException(status_code=400, detail="change_request_id is required")

    try:
        result, claimed_request = confirm_change_request(
            change_request_id=change_request_id,
            dry_run=req.dry_run,
            guard_token=x_guard_token,
            source="chat_confirm",
            approved_by=principal_subject(_principal),
        )
    except ChangeRequestServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    result = _normalize_generation_meta(result)
    result["session_id"] = req.session_id
    result["change_request_id"] = change_request_id

    action_type = claimed_request["action_type"]
    service_name = claimed_request["service_name"]

    if req.session_id:
        resolved_entities = claimed_request.get("resolved_entities") or {}
        upsert_chat_session_context(
            req.session_id,
            service_name=service_name or resolved_entities.get("service_name"),
            intent=action_type,
            version=resolved_entities.get("version"),
            env=resolved_entities.get("env"),
            namespace=resolved_entities.get("namespace"),
            cluster=resolved_entities.get("cluster"),
            region=resolved_entities.get("region"),
            action_target=resolved_entities.get("action_target"),
            time_window_minutes=resolved_entities.get("time_window_minutes"),
            clear_pending=True,
        )
    _log_generation_path(f"confirm:{change_request_id}", result)
    save_task_run(f"confirm:{change_request_id}", result)
    return ChatResponse(**result)
