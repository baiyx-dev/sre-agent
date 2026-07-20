import os

from backend.agents.orchestrator import execute_confirmed_action
from backend.security_execution_guard import (
    is_execution_guard_enabled,
    validate_execution_guard_token,
)
from backend.storage.repositories import (
    claim_change_request,
    complete_change_request,
    create_change_request,
    enqueue_change_request,
    get_change_job,
    get_change_request,
    save_execution_audit,
    redrive_failed_change_job,
)
from backend.services.commercial_service import production_write_entitled


class ChangeRequestServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def production_writes_enabled() -> bool:
    return os.getenv("SRE_PRODUCTION_WRITE_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _enforce_production_write_policy(*, dry_run: bool) -> None:
    environment = os.getenv("SRE_ENVIRONMENT", "development").strip().lower()
    if environment == "production" and not dry_run and not production_writes_enabled():
        raise ChangeRequestServiceError(
            403,
            "production writes are disabled; use dry_run or explicitly enable SRE_PRODUCTION_WRITE_ENABLED",
        )
    if environment == "production" and not dry_run and not production_write_entitled():
        raise ChangeRequestServiceError(
            402,
            "plan_entitlement_required: production_writes requires team or enterprise",
        )


def redrive_change_request(
    *,
    change_request_id: str,
    guard_token: str | None,
    source: str,
    redriven_by: str,
    reason: str,
) -> tuple[dict, dict]:
    stored_request = get_change_request(change_request_id)
    if not stored_request:
        raise ChangeRequestServiceError(404, "change request not found")
    if change_execution_mode() != "queued":
        raise ChangeRequestServiceError(409, "redrive requires queued execution mode")
    _enforce_production_write_policy(dry_run=False)

    if is_execution_guard_enabled():
        ok, guard_reason = validate_execution_guard_token(guard_token)
        if not ok:
            save_execution_audit(
                action=stored_request["action_type"],
                service_name=stored_request["service_name"],
                source=source,
                status="redrive_denied",
                reason=guard_reason,
                actor=redriven_by,
                change_request_id=change_request_id,
            )
            raise ChangeRequestServiceError(403, f"execution guard denied: {guard_reason}")

    try:
        max_redrives = max(
            1,
            min(int(os.getenv("SRE_CHANGE_JOB_MAX_REDRIVES", "3").strip()), 10),
        )
    except ValueError as exc:
        raise ChangeRequestServiceError(
            503,
            "SRE_CHANGE_JOB_MAX_REDRIVES must be an integer",
        ) from exc
    job, change_request, error = redrive_failed_change_job(
        change_request_id,
        redriven_by=redriven_by,
        reason=reason,
        max_redrives=max_redrives,
    )
    if error:
        status_code = 404 if error == "change_job_not_found" else 409
        raise ChangeRequestServiceError(status_code, error)
    save_execution_audit(
        action=stored_request["action_type"],
        service_name=stored_request["service_name"],
        source=source,
        status="redriven",
        reason=reason.strip()[:500],
        actor=redriven_by,
        change_request_id=change_request_id,
    )
    return job or {}, change_request or stored_request


def change_execution_mode() -> str:
    mode = os.getenv("SRE_CHANGE_EXECUTION_MODE", "synchronous").strip().lower()
    return mode if mode in {"synchronous", "queued"} else "invalid"


def submit_change_request(
    *,
    action_type: str,
    service_name: str,
    target_version: str | None,
    policy_decision: dict,
    resolved_entities: dict | None = None,
    source: str,
    requested_by: str,
) -> dict:
    if not policy_decision.get("allowed"):
        reason = policy_decision.get("summary") or "change policy denied"
        save_execution_audit(
            action=action_type,
            service_name=service_name,
            source=source,
            status="denied",
            reason=reason,
            actor=requested_by,
        )
        raise ChangeRequestServiceError(400, reason)

    change_request = create_change_request(
        action_type=action_type,
        service_name=service_name,
        target_version=target_version,
        policy_decision=policy_decision,
        resolved_entities=resolved_entities or {},
        requested_by=requested_by,
    )
    save_execution_audit(
        action=action_type,
        service_name=service_name,
        source=source,
        status="pending_confirmation",
        reason=f"change_request_id={change_request['change_request_id']}",
        actor=requested_by,
        change_request_id=change_request["change_request_id"],
    )
    return change_request


def confirm_change_request(
    *,
    change_request_id: str,
    dry_run: bool,
    guard_token: str | None,
    source: str,
    approved_by: str,
) -> tuple[dict, dict]:
    stored_request = get_change_request(change_request_id)
    if not stored_request:
        raise ChangeRequestServiceError(404, "change request not found")

    action_type = stored_request["action_type"]
    service_name = stored_request["service_name"]

    try:
        _enforce_production_write_policy(dry_run=dry_run)
    except ChangeRequestServiceError as exc:
        save_execution_audit(
            action=action_type,
            service_name=service_name,
            source=source,
            status="denied",
            reason=exc.detail,
            actor=approved_by,
            change_request_id=change_request_id,
        )
        raise

    if is_execution_guard_enabled():
        ok, reason = validate_execution_guard_token(guard_token)
        if not ok:
            save_execution_audit(
                action=action_type,
                service_name=service_name,
                source=source,
                status="denied",
                reason=reason,
                actor=approved_by,
                change_request_id=change_request_id,
            )
            raise ChangeRequestServiceError(403, f"execution guard denied: {reason}")

    if change_execution_mode() == "invalid":
        raise ChangeRequestServiceError(503, "unsupported SRE_CHANGE_EXECUTION_MODE")

    if change_execution_mode() == "queued":
        raw_max_attempts = os.getenv("SRE_CHANGE_JOB_MAX_ATTEMPTS", "1").strip()
        try:
            max_attempts = max(1, min(int(raw_max_attempts), 10))
        except ValueError as exc:
            raise ChangeRequestServiceError(
                503,
                "SRE_CHANGE_JOB_MAX_ATTEMPTS must be an integer",
            ) from exc
        queued_request, queue_error = enqueue_change_request(
            change_request_id,
            approved_by=approved_by,
            dry_run=dry_run,
            max_attempts=max_attempts,
        )
        if queue_error:
            status_code = 410 if queue_error == "change_request_expired" else 409
            raise ChangeRequestServiceError(status_code, queue_error)
        job = get_change_job(change_request_id) or {}
        result = {
            "intent": action_type,
            "steps": [
                {
                    "step": 1,
                    "action": "enqueue_change_request",
                    "result": job,
                }
            ],
            "final_answer": f"变更请求 {change_request_id} 已进入执行队列。",
            "policy_decision": stored_request.get("policy_decision") or {},
            "execution_mode": "queued",
            "requires_confirmation": False,
            "pending_action": None,
            "change_request_id": change_request_id,
            "job_id": job.get("job_id"),
        }
        save_execution_audit(
            action=action_type,
            service_name=service_name,
            source=source,
            status="queued",
            reason=f"job_id={job.get('job_id')}",
            actor=approved_by,
            change_request_id=change_request_id,
        )
        return result, queued_request or stored_request

    claimed_request, claim_error = claim_change_request(
        change_request_id,
        approved_by=approved_by,
    )
    if claim_error:
        status_code = 410 if claim_error == "change_request_expired" else 409
        raise ChangeRequestServiceError(status_code, claim_error)

    pending_action = dict(claimed_request or {})
    pending_action["dry_run"] = dry_run
    try:
        result = execute_confirmed_action(pending_action)
    except Exception:
        complete_change_request(
            change_request_id,
            status="failed",
            result={"error": "unhandled_execution_error"},
        )
        save_execution_audit(
            action=action_type,
            service_name=service_name,
            source=source,
            status="failed",
            reason="unhandled_execution_error",
            actor=approved_by,
            change_request_id=change_request_id,
        )
        raise

    result["change_request_id"] = change_request_id
    if dry_run:
        completion_status = "dry_run"
    elif result.get("execution_mode") == "denied":
        completion_status = "denied"
    elif result.get("execution_mode") == "failed":
        completion_status = "failed"
    else:
        completion_status = "executed"

    complete_change_request(
        change_request_id,
        status=completion_status,
        result=result,
    )
    save_execution_audit(
        action=action_type,
        service_name=service_name,
        source=source,
        status=completion_status,
        reason=(result.get("policy_decision") or {}).get("summary"),
        actor=approved_by,
        change_request_id=change_request_id,
    )
    return result, claimed_request or stored_request
