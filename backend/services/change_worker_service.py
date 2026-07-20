import logging

from backend.agents.orchestrator import execute_confirmed_action
from backend.storage.repositories import (
    claim_next_change_job,
    complete_change_job,
    retry_or_fail_change_job,
    save_execution_audit,
)

logger = logging.getLogger("sre-agent.change-worker")


def process_next_change_job(worker_id: str) -> dict | None:
    job, change_request = claim_next_change_job(worker_id)
    if not job or not change_request:
        return None

    change_request_id = change_request["change_request_id"]
    action_type = change_request["action_type"]
    service_name = change_request["service_name"]
    pending_action = dict(change_request)
    pending_action["dry_run"] = job["dry_run"]
    try:
        result = execute_confirmed_action(pending_action)
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        retrying = retry_or_fail_change_job(
            job["job_id"],
            change_request_id,
            error_message=error_message,
        )
        save_execution_audit(
            action=action_type,
            service_name=service_name,
            source="change_worker",
            status="retrying" if retrying else "unknown",
            reason=error_message[:500],
            actor=change_request.get("approved_by") or worker_id,
            change_request_id=change_request_id,
        )
        logger.exception(
            "change_job_execution_exception worker_id=%s job_id=%s change_request_id=%s retrying=%s",
            worker_id,
            job["job_id"],
            change_request_id,
            retrying,
        )
        return {
            "job_id": job["job_id"],
            "change_request_id": change_request_id,
            "status": "retrying" if retrying else "unknown",
        }

    result["change_request_id"] = change_request_id
    if job["dry_run"]:
        completion_status = "dry_run"
    elif result.get("execution_mode") == "denied":
        completion_status = "denied"
    elif result.get("execution_mode") == "failed":
        completion_status = "failed"
    else:
        completion_status = "executed"
    complete_change_job(
        job["job_id"],
        change_request_id,
        completion_status=completion_status,
        result=result,
    )
    save_execution_audit(
        action=action_type,
        service_name=service_name,
        source="change_worker",
        status=completion_status,
        reason=(result.get("policy_decision") or {}).get("summary"),
        actor=change_request.get("approved_by") or worker_id,
        change_request_id=change_request_id,
    )
    return {
        "job_id": job["job_id"],
        "change_request_id": change_request_id,
        "status": completion_status,
        "result": result,
    }
