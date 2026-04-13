from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend.tools.alert_tool import get_recent_alerts
from backend.tools.deploy_tool import deploy_service
from backend.tools.rollback_tool import rollback_service
from backend.storage.repositories import generate_postmortem, get_task_timeline
from backend.llm.provider import generate_postmortem_narrative
from backend.security_execution_guard import is_execution_guard_enabled, validate_execution_guard_token
from backend.storage.repositories import save_execution_audit
from backend.services.benchmark_service import list_benchmark_scenarios, run_benchmark, run_replay_scenario
from backend.services.policy_service import build_execution_preview, evaluate_action_policy

router = APIRouter(tags=["incidents"])


class DeployRequest(BaseModel):
    service_name: str
    new_version: str
    dry_run: bool = False


class RollbackRequest(BaseModel):
    service_name: str
    dry_run: bool = False


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
def deploy(req: DeployRequest, x_guard_token: str | None = Header(default=None, alias="X-Guard-Token")):
    policy_decision = evaluate_action_policy("deploy", req.service_name, target_version=req.new_version)
    if req.dry_run:
        preview = build_execution_preview("deploy", req.service_name, target_version=req.new_version)
        save_execution_audit(
            action="deploy",
            service_name=req.service_name,
            source="incidents_api",
            status="dry_run",
            reason=preview["message"],
        )
        return preview
    if not policy_decision["allowed"]:
        save_execution_audit(action="deploy", service_name=req.service_name, source="incidents_api", status="denied", reason=policy_decision["summary"])
        raise HTTPException(status_code=400, detail=policy_decision["summary"])
    if is_execution_guard_enabled():
        ok, reason = validate_execution_guard_token(x_guard_token)
        if not ok:
            save_execution_audit(action="deploy", service_name=req.service_name, source="incidents_api", status="denied", reason=reason)
            raise HTTPException(status_code=403, detail=f"execution guard denied: {reason}")

    result = deploy_service(req.service_name, req.new_version)
    if not result["success"]:
        raise HTTPException(status_code=404, detail=result["message"])
    result["policy_decision"] = policy_decision
    result["execution_mode"] = "execute"
    save_execution_audit(action="deploy", service_name=req.service_name, source="incidents_api", status="executed", reason=policy_decision["summary"])
    return result


@router.post("/rollback")
def rollback(req: RollbackRequest, x_guard_token: str | None = Header(default=None, alias="X-Guard-Token")):
    policy_decision = evaluate_action_policy("rollback", req.service_name)
    if req.dry_run:
        preview = build_execution_preview("rollback", req.service_name)
        save_execution_audit(
            action="rollback",
            service_name=req.service_name,
            source="incidents_api",
            status="dry_run",
            reason=preview["message"],
        )
        return preview
    if not policy_decision["allowed"]:
        save_execution_audit(action="rollback", service_name=req.service_name, source="incidents_api", status="denied", reason=policy_decision["summary"])
        raise HTTPException(status_code=400, detail=policy_decision["summary"])
    if is_execution_guard_enabled():
        ok, reason = validate_execution_guard_token(x_guard_token)
        if not ok:
            save_execution_audit(action="rollback", service_name=req.service_name, source="incidents_api", status="denied", reason=reason)
            raise HTTPException(status_code=403, detail=f"execution guard denied: {reason}")

    result = rollback_service(req.service_name)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    result["policy_decision"] = policy_decision
    result["execution_mode"] = "execute"
    save_execution_audit(action="rollback", service_name=req.service_name, source="incidents_api", status="executed", reason=policy_decision["summary"])
    return result

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
