from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.tools.alert_tool import get_recent_alerts
from backend.storage.repositories import generate_postmortem, get_task_timeline
from backend.llm.provider import generate_postmortem_narrative
from backend.storage.repositories import save_execution_audit
from backend.services.benchmark_service import list_benchmark_scenarios, run_benchmark, run_replay_scenario
from backend.services.policy_service import build_execution_preview, evaluate_action_policy
from backend.services.change_request_service import ChangeRequestServiceError, submit_change_request
from backend.security_auth import principal_subject, require_operator, require_viewer
from backend.services.incident_service import (
    IncidentServiceError,
    correlate_alerts,
    get_incident,
    list_incidents,
    update_incident,
)

router = APIRouter(tags=["incidents"], dependencies=[Depends(require_viewer)])


class DeployRequest(BaseModel):
    service_name: str
    new_version: str
    dry_run: bool = False


class RollbackRequest(BaseModel):
    service_name: str
    dry_run: bool = False


class IncidentUpdateRequest(BaseModel):
    status: str | None = None
    owner: str | None = None
    summary: str | None = None


@router.get("/alerts")
def alerts(service_name: str | None = None, unresolved_only: bool = True, limit: int = 10):
    return {
        "alerts": get_recent_alerts(
            service_name=service_name,
            unresolved_only=unresolved_only,
            limit=limit
        )
    }


@router.post("/deploy")
def deploy(
    req: DeployRequest,
    _principal=Depends(require_operator),
):
    policy_decision = evaluate_action_policy("deploy", req.service_name, target_version=req.new_version)
    if req.dry_run:
        preview = build_execution_preview("deploy", req.service_name, target_version=req.new_version)
        save_execution_audit(
            action="deploy",
            service_name=req.service_name,
            source="incidents_api",
            status="dry_run",
            reason=preview["message"],
            actor=principal_subject(_principal),
        )
        return preview
    try:
        change_request = submit_change_request(
            action_type="deploy",
            service_name=req.service_name,
            target_version=req.new_version,
            policy_decision=policy_decision,
            resolved_entities={"service_name": req.service_name, "version": req.new_version},
            source="incidents_api",
            requested_by=principal_subject(_principal),
        )
    except ChangeRequestServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {
        "mode": "pending_confirmation",
        "execution_mode": "pending_confirmation",
        "requires_confirmation": True,
        "change_request_id": change_request["change_request_id"],
        "change_request": change_request,
        "policy_decision": policy_decision,
        "confirmation_endpoint": f"/changes/{change_request['change_request_id']}/confirm",
    }


@router.get("/incidents")
def incidents(
    status: str | None = None,
    service_name: str | None = None,
    limit: int = 100,
):
    try:
        items = list_incidents(
            status=status,
            service_name=service_name,
            limit=limit,
        )
    except IncidentServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"incidents": items}


@router.get("/incidents/{incident_id}")
def incident_detail(incident_id: str):
    item = get_incident(incident_id)
    if not item:
        raise HTTPException(status_code=404, detail="incident not found")
    return {"incident": item}


@router.post("/incidents/correlate")
def correlate_current_alerts(_principal=Depends(require_operator)):
    current_alerts = get_recent_alerts(unresolved_only=True, limit=500)
    items = correlate_alerts(
        current_alerts,
        actor=principal_subject(_principal),
        source="observability",
    )
    return {
        "processed_alerts": len(current_alerts),
        "touched_incidents": len(items),
        "incidents": items,
    }


@router.patch("/incidents/{incident_id}")
def change_incident(
    incident_id: str,
    req: IncidentUpdateRequest,
    _principal=Depends(require_operator),
):
    try:
        item = update_incident(
            incident_id,
            actor=principal_subject(_principal),
            status=req.status,
            owner=req.owner,
            summary=req.summary,
        )
    except IncidentServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"incident": item}


@router.post("/rollback")
def rollback(
    req: RollbackRequest,
    _principal=Depends(require_operator),
):
    policy_decision = evaluate_action_policy("rollback", req.service_name)
    if req.dry_run:
        preview = build_execution_preview("rollback", req.service_name)
        save_execution_audit(
            action="rollback",
            service_name=req.service_name,
            source="incidents_api",
            status="dry_run",
            reason=preview["message"],
            actor=principal_subject(_principal),
        )
        return preview
    try:
        change_request = submit_change_request(
            action_type="rollback",
            service_name=req.service_name,
            target_version=None,
            policy_decision=policy_decision,
            resolved_entities={"service_name": req.service_name},
            source="incidents_api",
            requested_by=principal_subject(_principal),
        )
    except ChangeRequestServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {
        "mode": "pending_confirmation",
        "execution_mode": "pending_confirmation",
        "requires_confirmation": True,
        "change_request_id": change_request["change_request_id"],
        "change_request": change_request,
        "policy_decision": policy_decision,
        "confirmation_endpoint": f"/changes/{change_request['change_request_id']}/confirm",
    }

@router.get("/timeline")
def timeline(limit: int = 20):
    return {"timeline": get_task_timeline(limit=limit)}


@router.get("/postmortem")
def postmortem(task_run_id: int, limit: int = 50):
    postmortem_data = generate_postmortem(task_run_id=task_run_id, limit=limit)
    fallback_narrative = (
        f"复盘摘要：{postmortem_data.get('summary', '-')}"
        f" 现象：{'; '.join(postmortem_data.get('symptoms', [])[:2]) or '-'}。"
        f" 根因判断：{postmortem_data.get('likely_root_cause', '-')}。"
        f" 处理动作：{'; '.join(postmortem_data.get('actions_taken', [])[:2]) or '-'}。"
        f" 后续改进：{'; '.join(postmortem_data.get('follow_ups', [])[:2]) or '-'}。"
    )
    postmortem_data["narrative_summary"] = generate_postmortem_narrative(
        postmortem=postmortem_data,
        fallback_summary=fallback_narrative,
    )
    return {"postmortem": postmortem_data}


@router.get("/benchmark/scenarios")
def benchmark_scenarios():
    return {"scenarios": list_benchmark_scenarios()}


@router.get("/benchmark/run")
def benchmark_run():
    return run_benchmark()


@router.get("/benchmark/replay/{scenario_id}")
def benchmark_replay(scenario_id: str):
    replay = run_replay_scenario(scenario_id)
    if not replay:
        raise HTTPException(status_code=404, detail="benchmark scenario not found")
    return replay
