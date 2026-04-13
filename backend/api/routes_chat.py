from fastapi import APIRouter, Header, HTTPException

from backend.agents.orchestrator import execute_confirmed_action, run_agent
from backend.schemas.chat import ChatRequest, ChatResponse, ConfirmActionRequest
from backend.storage.repositories import (
    get_chat_session_context,
    save_execution_audit,
    save_task_run,
    upsert_chat_session_context,
)
from backend.security_execution_guard import is_execution_guard_enabled, validate_execution_guard_token

router = APIRouter(tags=["chat"])


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
def chat(req: ChatRequest):
    session_context = get_chat_session_context(req.session_id) if req.session_id else None
    result = run_agent(
        req.message,
        confirm=req.confirm,
        pending_action=req.pending_action,
        session_context=session_context,
    )
    result = _normalize_generation_meta(result)
    result["session_id"] = req.session_id

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
def confirm_action(req: ConfirmActionRequest, x_guard_token: str | None = Header(default=None, alias="X-Guard-Token")):
    action_type = req.pending_action.get("action_type") if req.pending_action else None
    service_name = req.pending_action.get("service_name") if req.pending_action else None

    if is_execution_guard_enabled() and action_type == "rollback":
        ok, reason = validate_execution_guard_token(x_guard_token)
        if not ok:
            save_execution_audit(action="rollback", service_name=service_name, source="chat_confirm", status="denied", reason=reason)
            raise HTTPException(status_code=403, detail=f"execution guard denied: {reason}")

    pending_action = dict(req.pending_action or {})
    pending_action["dry_run"] = req.dry_run
    result = execute_confirmed_action(pending_action)
    result = _normalize_generation_meta(result)
    result["session_id"] = req.session_id
    if req.session_id:
        resolved_entities = pending_action.get("resolved_entities") or {}
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
    _log_generation_path(f"confirm:{req.pending_action}", result)
    save_execution_audit(
        action=action_type or "unknown",
        service_name=service_name,
        source="chat_confirm",
        status="executed" if not req.dry_run else "dry_run",
        reason=(result.get("policy_decision") or {}).get("summary"),
    )
    return ChatResponse(**result)
