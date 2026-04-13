from backend.storage.repositories import get_recent_deploy_context
from backend.tools.alert_tool import get_recent_alerts
from backend.tools.external_data_source import get_external_k8s_observability
from backend.tools.service_tool import get_service_status


def evaluate_action_policy(
    action_type: str,
    service_name: str | None,
    target_version: str | None = None,
) -> dict:
    checks = []
    reasons = []

    if not service_name:
        return {
            "action_type": action_type,
            "service_name": service_name,
            "allowed": False,
            "risk_level": "high",
            "requires_confirmation": False,
            "recommended_mode": "dry_run",
            "summary": "缺少服务名，无法进行执行前评估。",
            "reasons": ["missing_service_name"],
            "checks": [],
        }

    service = get_service_status(service_name)
    service_exists = service is not None
    checks.append({
        "check": "service_exists",
        "passed": service_exists,
        "details": f"service={service_name}",
    })
    if not service_exists:
        return {
            "action_type": action_type,
            "service_name": service_name,
            "allowed": False,
            "risk_level": "high",
            "requires_confirmation": False,
            "recommended_mode": "dry_run",
            "summary": f"未找到服务 {service_name}，策略评估拒绝执行。",
            "reasons": ["service_not_found"],
            "checks": checks,
        }

    alerts = get_recent_alerts(service_name=service_name, unresolved_only=True, limit=20)
    open_alert_count = len(alerts)
    checks.append({
        "check": "open_alerts",
        "passed": open_alert_count == 0,
        "details": f"open_alert_count={open_alert_count}",
    })

    status_value = service.get("status")
    error_rate = service.get("error_rate", 0.0) or 0.0
    k8s_observability = get_external_k8s_observability(service_name)
    k8s_signals = _extract_k8s_policy_signals(k8s_observability)
    checks.extend(_build_k8s_checks(k8s_signals))

    risk_level = _derive_risk_level(
        status_value,
        error_rate,
        open_alert_count,
        rollout_status=k8s_signals["rollout_status"],
        unhealthy_pod_count=k8s_signals["unhealthy_pod_count"],
        restarting_pod_count=k8s_signals["restarting_pod_count"],
        warning_event_count=k8s_signals["warning_event_count"],
    )
    recommended_mode = "dry_run" if risk_level == "high" else "execute"
    requires_confirmation = action_type == "rollback" or risk_level == "high"

    if action_type == "deploy":
        version_present = bool(target_version)
        checks.append({
            "check": "target_version_present",
            "passed": version_present,
            "details": f"target_version={target_version}",
        })
        if not version_present:
            reasons.append("missing_target_version")

        version_changed = bool(target_version and target_version != service.get("version"))
        checks.append({
            "check": "target_version_differs",
            "passed": version_changed,
            "details": f"current={service.get('version')} target={target_version}",
        })
        if version_present and not version_changed:
            reasons.append("target_version_matches_current")

    if action_type == "rollback":
        history = get_recent_deploy_context(service_name, limit=1)
        has_history = len(history) > 0
        checks.append({
            "check": "rollback_history_available",
            "passed": has_history,
            "details": f"history_count={len(history)}",
        })
        if not has_history:
            reasons.append("no_rollback_history")

    allowed = all(item["passed"] for item in checks if item["check"] in {
        "service_exists",
        "target_version_present",
        "target_version_differs",
        "rollback_history_available",
    })

    if open_alert_count > 0:
        reasons.append("service_has_open_alerts")
    if status_value in ("degraded", "down"):
        reasons.append("service_not_healthy")
    if error_rate > 5:
        reasons.append("error_rate_above_threshold")
    if k8s_signals["rollout_status"] in {"progressing", "degraded"}:
        reasons.append("k8s_rollout_unstable")
    if k8s_signals["unhealthy_pod_count"] > 0 or k8s_signals["restarting_pod_count"] > 0:
        reasons.append("k8s_runtime_unhealthy")
    if k8s_signals["warning_event_count"] > 0:
        reasons.append("k8s_warning_events_present")

    summary = _build_policy_summary(
        action_type=action_type,
        service_name=service_name,
        service=service,
        risk_level=risk_level,
        open_alert_count=open_alert_count,
        allowed=allowed,
        target_version=target_version,
        k8s_signals=k8s_signals,
    )

    return {
        "action_type": action_type,
        "service_name": service_name,
        "allowed": allowed,
        "risk_level": risk_level,
        "requires_confirmation": requires_confirmation if allowed else False,
        "recommended_mode": recommended_mode,
        "summary": summary,
        "reasons": reasons,
        "checks": checks,
        "k8s_signals": k8s_signals,
    }


def build_execution_preview(
    action_type: str,
    service_name: str,
    target_version: str | None = None,
) -> dict:
    policy = evaluate_action_policy(action_type, service_name, target_version=target_version)
    if not policy["allowed"]:
        return {
            "ok": False,
            "mode": "dry_run",
            "policy_decision": policy,
            "preview_steps": [],
            "message": policy["summary"],
        }

    preview_steps = []
    if action_type == "deploy":
        preview_steps = [
            f"检查服务 {service_name} 当前版本和运行状态",
            "检查 Deployment rollout、Pod 就绪状态和近期 Warning 事件",
            f"验证目标版本 {target_version} 与当前版本是否不同",
            "根据风险等级决定是否继续执行或先做 dry-run",
            "如执行成功，记录部署历史与审计事件",
        ]
    elif action_type == "rollback":
        preview_steps = [
            f"检查服务 {service_name} 当前状态和最近一次部署记录",
            "检查当前 Pod 健康、重启次数和 BackOff/CrashLoop 事件",
            "确认存在可回滚版本",
            "回滚后刷新服务状态并关闭相关开放告警",
            "记录回滚历史和执行审计",
        ]

    return {
        "ok": True,
        "mode": "dry_run",
        "policy_decision": policy,
        "preview_steps": preview_steps,
        "message": policy["summary"],
    }


def _derive_risk_level(
    status_value: str | None,
    error_rate: float,
    open_alert_count: int,
    rollout_status: str | None = None,
    unhealthy_pod_count: int = 0,
    restarting_pod_count: int = 0,
    warning_event_count: int = 0,
) -> str:
    if (
        status_value in ("degraded", "down")
        or error_rate > 5
        or open_alert_count > 0
        or rollout_status in {"progressing", "degraded"}
        or unhealthy_pod_count > 0
        or restarting_pod_count > 0
        or warning_event_count > 0
    ):
        return "high"
    if error_rate > 1:
        return "medium"
    return "low"


def _build_policy_summary(
    action_type: str,
    service_name: str,
    service: dict,
    risk_level: str,
    open_alert_count: int,
    allowed: bool,
    target_version: str | None,
    k8s_signals: dict,
) -> str:
    action_label = "部署" if action_type == "deploy" else "回滚"
    current_version = service.get("version", "-")
    status_value = service.get("status", "unknown")
    rollout_status = k8s_signals.get("rollout_status") or "unknown"
    unhealthy_pod_count = k8s_signals.get("unhealthy_pod_count", 0)
    restarting_pod_count = k8s_signals.get("restarting_pod_count", 0)
    warning_event_count = k8s_signals.get("warning_event_count", 0)
    if not allowed:
        return (
            f"{action_label}策略评估未通过。服务 {service_name} 当前版本 {current_version}，"
            f"状态 {status_value}，请先补齐前置条件后再执行。"
        )
    target_text = f"，目标版本 {target_version}" if action_type == "deploy" and target_version else ""
    return (
        f"{action_label}策略评估通过。服务 {service_name} 当前版本 {current_version}{target_text}，"
        f"状态 {status_value}，开放告警 {open_alert_count} 条，"
        f"K8s rollout {rollout_status}，异常 Pod {unhealthy_pod_count} 个，"
        f"重启 Pod {restarting_pod_count} 个，Warning 事件 {warning_event_count} 条，"
        f"风险等级 {risk_level}。"
    )


def _extract_k8s_policy_signals(k8s_observability: dict | None) -> dict:
    summary = (k8s_observability or {}).get("summary", {})
    rollout = (k8s_observability or {}).get("rollout", {})
    events = (k8s_observability or {}).get("events", [])
    warning_event_count = sum(1 for item in events if item.get("type") == "Warning")
    return {
        "available": bool(k8s_observability),
        "rollout_status": rollout.get("rollout_status"),
        "unhealthy_pod_count": summary.get("unhealthy_pods", 0) or 0,
        "restarting_pod_count": summary.get("restarting_pods", 0) or 0,
        "warning_event_count": warning_event_count,
    }


def _build_k8s_checks(k8s_signals: dict) -> list[dict]:
    if not k8s_signals.get("available"):
        return [{
            "check": "k8s_observability_available",
            "passed": False,
            "details": "k8s_observability=missing",
        }]
    return [
        {
            "check": "k8s_rollout_healthy",
            "passed": k8s_signals["rollout_status"] not in {"progressing", "degraded"},
            "details": f"rollout_status={k8s_signals['rollout_status']}",
        },
        {
            "check": "k8s_pods_healthy",
            "passed": k8s_signals["unhealthy_pod_count"] == 0 and k8s_signals["restarting_pod_count"] == 0,
            "details": (
                f"unhealthy_pods={k8s_signals['unhealthy_pod_count']} "
                f"restarting_pods={k8s_signals['restarting_pod_count']}"
            ),
        },
        {
            "check": "k8s_warning_events",
            "passed": k8s_signals["warning_event_count"] == 0,
            "details": f"warning_event_count={k8s_signals['warning_event_count']}",
        },
    ]
