from backend.agents.intent_router import detect_intent, extract_entities
from backend.tools.alert_tool import get_recent_alerts
from backend.tools.deploy_tool import deploy_service
from backend.tools.logs_tool import get_recent_logs
from backend.tools.metrics_tool import get_service_metrics
from backend.tools.rollback_tool import rollback_service
from backend.tools.service_tool import get_service_status, list_services
from backend.tools.external_data_source import get_external_k8s_observability
from backend.llm.provider import generate_final_answer, generate_troubleshoot_assessment
from backend.storage.repositories import get_recent_deploy_context
from backend.services.policy_service import evaluate_action_policy, build_execution_preview


def _load_pending_json_list(session_context: dict | None, key: str) -> list[str]:
    raw = (session_context or {}).get(key)
    if isinstance(raw, str) and raw:
        try:
            import json
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except Exception:
            return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _resolve_service_from_pending_options(message: str, session_context: dict | None) -> str | None:
    options = _load_pending_json_list(session_context, "pending_options")
    if not options:
        return None

    text = message.strip().lower()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(options):
            return options[idx]

    for option in options:
        normalized = option.lower()
        if text == normalized or normalized in text or text in normalized:
            return option
    return None


def _looks_like_clarification_reply(message: str, entities: dict, session_context: dict | None) -> bool:
    pending_missing_fields = _load_pending_json_list(session_context, "pending_missing_fields")

    if not pending_missing_fields:
        pending_missing_fields = ["service_name", "version"]

    for field in pending_missing_fields:
        if entities.get(field) is not None:
            return True

    if "service_name" in pending_missing_fields and _resolve_service_from_pending_options(message, session_context):
        return True

    short_message = len(message.strip()) <= 20
    return short_message and any(token in message for token in ["这个", "那个", "刚才", "上一个"])


def _build_clarification_response(
    intent: str,
    entities: dict,
    resolved_from_session: dict,
    missing_fields: list[str],
    question: str,
    options: list[str] | None = None,
) -> dict:
    return {
        "intent": intent,
        "steps": [],
        "final_answer": (
            question if not options
            else f"{question} 可选："
            + "；".join(f"{idx + 1}. {item}" for idx, item in enumerate(options[:5]))
        ),
        "requires_clarification": True,
        "clarification_question": question,
        "clarification_options": options,
        "pending_clarification": {
            "intent": intent,
            "missing_fields": missing_fields,
        },
        "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
    }


def _candidate_service_options(intent: str) -> list[str]:
    services = [svc for svc in list_services() if isinstance(svc, dict) and svc.get("name")]
    if intent == "rollback":
        services.sort(
            key=lambda svc: (
                0 if get_recent_deploy_context(svc["name"], limit=1) else 1,
                svc["name"],
            )
        )
    elif intent == "troubleshoot":
        alerts = {item.get("service") for item in get_recent_alerts(limit=10) if isinstance(item, dict)}
        services.sort(
            key=lambda svc: (
                0 if svc["name"] in alerts else 1,
                svc["name"],
            )
        )
    else:
        services.sort(key=lambda svc: svc["name"])
    return [svc["name"] for svc in services[:5]]


def _severity_from_metrics(error_rate: float | int | None, has_alerts: bool, status_value: str | None) -> str:
    if status_value in ("down", "degraded"):
        return "high"
    if error_rate is not None and error_rate > 10:
        return "high"
    if has_alerts or (error_rate is not None and error_rate > 1):
        return "medium"
    return "low"


def _build_fallback_troubleshoot_assessment(
    service_name: str,
    alerts: list,
    status: dict,
    metrics: dict,
    logs: list,
    recent_changes: list,
    k8s_observability: dict | None = None,
) -> dict:
    status_value = (status or {}).get("status", "unknown")
    error_rate = (metrics or {}).get("error_rate")
    has_db_timeout = any(
        isinstance(log, dict) and "database connection timeout" in str(log.get("message", "")).lower()
        for log in (logs or [])
    )
    has_probe_failure = any(
        isinstance(log, dict) and "health probe failed" in str(log.get("message", "")).lower()
        for log in (logs or [])
    )
    recent_failed_change = any(
        isinstance(change, dict) and str(change.get("status", "")).lower() != "success"
        for change in (recent_changes or [])
    )
    rollout = (k8s_observability or {}).get("rollout") or {}
    rollout_status = rollout.get("rollout_status")
    pods = (k8s_observability or {}).get("pods") or []
    events = (k8s_observability or {}).get("events") or []
    restarting_pods = [pod for pod in pods if int(pod.get("restart_count") or 0) > 0]
    unhealthy_pods = [pod for pod in pods if pod.get("phase") != "Running" or not pod.get("ready")]
    crashloop_events = [
        event for event in events
        if "backoff" in str(event.get("reason", "")).lower()
        or "crashloop" in str(event.get("message", "")).lower()
    ]

    evidence = [
        f"服务 {service_name} 当前状态为 {status_value}",
        f"当前错误率为 {error_rate if error_rate is not None else '-'}%",
    ]
    if alerts:
        evidence.append(f"发现 {len(alerts)} 条近期未恢复告警")
    if has_db_timeout:
        evidence.append("日志中出现 database connection timeout")
    if has_probe_failure:
        evidence.append("健康检查日志显示服务可用性下降")
    if recent_changes:
        evidence.append(f"最近检测到 {len(recent_changes)} 次相关变更记录")
    if rollout_status:
        evidence.append(f"K8s rollout 状态为 {rollout_status}")
    if unhealthy_pods:
        evidence.append(f"发现 {len(unhealthy_pods)} 个不健康 Pod")
    if restarting_pods:
        evidence.append(f"发现 {len(restarting_pods)} 个 Pod 出现重启")
    if crashloop_events:
        evidence.append("K8s event 中出现 BackOff / CrashLoop 迹象")

    hypotheses = []
    if has_db_timeout:
        hypotheses.append({
            "hypothesis": "数据库连接池或下游依赖异常",
            "confidence": "high" if status_value != "running" or (error_rate or 0) > 1 else "medium",
            "rationale": "错误日志明确出现 database connection timeout，与依赖异常特征吻合。",
        })
    if recent_failed_change or ((error_rate or 0) > 10 and recent_changes):
        hypotheses.append({
            "hypothesis": "最近变更引入回归",
            "confidence": "medium",
            "rationale": "异常与近期变更时间窗口重叠，且当前错误率偏高。",
        })
    if alerts or has_probe_failure or status_value in ("down", "degraded"):
        hypotheses.append({
            "hypothesis": "服务自身实例异常或健康检查失败",
            "confidence": "high" if status_value in ("down", "degraded") else "medium",
            "rationale": "当前状态、告警和探测结果均显示实例稳定性存在问题。",
        })
    if rollout_status in ("degraded", "progressing") or unhealthy_pods or crashloop_events:
        hypotheses.append({
            "hypothesis": "Kubernetes rollout 或 Pod 运行态异常",
            "confidence": "high" if crashloop_events or unhealthy_pods else "medium",
            "rationale": "rollout、Pod 和事件流都显示发布或实例生命周期存在异常。",
        })
    if not hypotheses:
        hypotheses.append({
            "hypothesis": "暂未发现明确故障根因",
            "confidence": "low",
            "rationale": "当前状态和关键指标基本正常，暂时缺少支持异常的强证据。",
        })

    missing_signals = []
    if not recent_changes:
        missing_signals.append("缺少变更流水和发布记录，无法确认是否由最近发布引入。")
    if not logs:
        missing_signals.append("缺少应用日志样本，无法进一步定位异常链路。")
    if metrics and metrics.get("cpu") == 0 and metrics.get("memory") == 0:
        missing_signals.append("当前没有真实资源指标，建议接入 Prometheus 或云监控。")
    if not alerts:
        missing_signals.append("当前未接入更多上下游告警，影响关联分析的完整性。")
    if not k8s_observability:
        missing_signals.append("当前未接入 Kubernetes rollout / Pod / Event 级观测。")

    next_actions = []
    if hypotheses[0]["hypothesis"] == "数据库连接池或下游依赖异常":
        next_actions.extend([
            "优先检查数据库连接数、连接池饱和度和下游依赖超时情况。",
            "对超时链路执行一次按时间窗口聚合的日志检索，确认是否集中在单个实例。",
        ])
    if recent_changes or recent_failed_change:
        next_actions.append("核对最近一次发布差异、发布批次和异常发生时间是否重叠。")
    if status_value in ("down", "degraded"):
        next_actions.append("先确认实例存活和健康检查，再决定是否需要回滚或重启。")
    if rollout_status in ("degraded", "progressing") or unhealthy_pods:
        next_actions.append("检查 Deployment rollout、异常 Pod 状态和最近的 Kubernetes 事件。")
    if not next_actions:
        next_actions.append("继续观察 5-10 分钟并补充更多日志和指标后再做结论。")

    severity = _severity_from_metrics(error_rate, bool(alerts), status_value)
    confidence = "high" if hypotheses[0]["confidence"] == "high" else "medium" if severity != "low" else "low"
    summary = (
        f"{service_name} 当前处于 {severity} 风险等级。"
        f" 最可能的方向是 {hypotheses[0]['hypothesis']}，"
        f"主要依据包括：{'；'.join(evidence[:3])}。"
    )

    return {
        "summary": summary,
        "severity_assessment": severity,
        "confidence": confidence,
        "evidence": evidence[:5],
        "hypotheses": hypotheses[:3],
        "missing_signals": missing_signals[:5],
        "next_actions": next_actions[:5],
    }


def _format_troubleshoot_final_answer(service_name: str, assessment: dict) -> str:
    lead_hypothesis = assessment.get("hypotheses", [{}])[0]
    next_action = (assessment.get("next_actions") or ["继续补充观测数据后再做判断。"])[0]
    return (
        f"排查完成：{service_name} 当前风险等级为 {assessment.get('severity_assessment', 'unknown')}，"
        f"判断置信度 {assessment.get('confidence', 'unknown')}。"
        f" 核心结论：{assessment.get('summary', '-')}"
        f" 首要怀疑方向：{lead_hypothesis.get('hypothesis', '-')}。"
        f" 建议下一步：{next_action}"
    )


def _extract_key_status(steps: list) -> dict:
    key_status = {}

    for step in steps:
        action = step.get("action")
        result = step.get("result")

        if action == "get_service_status" and isinstance(result, dict):
            key_status["service_status"] = result
        elif action == "get_service_metrics" and isinstance(result, dict):
            key_status["service_metrics"] = result
        elif action in ("deploy_service", "rollback_service") and isinstance(result, dict):
            key_status[action] = result

    return key_status


def _summarize_with_llm(user_message: str, intent: str, steps: list, fallback_answer: str) -> tuple[str, dict]:
    key_status = _extract_key_status(steps)
    return generate_final_answer(
        user_message=user_message,
        intent=intent,
        steps=steps,
        key_status=key_status,
        fallback_answer=fallback_answer,
    )


def _merge_session_entities(entities: dict, session_context: dict | None) -> tuple[dict, dict]:
    merged = dict(entities or {})
    session_context = session_context or {}
    resolved_from_session = {
        "service_name": False,
        "action_target": False,
        "version": False,
        "env": False,
        "namespace": False,
        "cluster": False,
        "region": False,
        "time_window_minutes": False,
    }

    if not merged.get("service_name") and session_context.get("last_service_name"):
        merged["service_name"] = session_context["last_service_name"]
        resolved_from_session["service_name"] = True

    if not merged.get("action_target") and session_context.get("last_action_target"):
        merged["action_target"] = session_context["last_action_target"]
        resolved_from_session["action_target"] = True

    if not merged.get("version") and session_context.get("last_version"):
        merged["version"] = session_context["last_version"]
        resolved_from_session["version"] = True

    if not merged.get("env") and session_context.get("last_env"):
        merged["env"] = session_context["last_env"]
        resolved_from_session["env"] = True

    if not merged.get("namespace") and session_context.get("last_namespace"):
        merged["namespace"] = session_context["last_namespace"]
        resolved_from_session["namespace"] = True

    if not merged.get("cluster") and session_context.get("last_cluster"):
        merged["cluster"] = session_context["last_cluster"]
        resolved_from_session["cluster"] = True

    if not merged.get("region") and session_context.get("last_region"):
        merged["region"] = session_context["last_region"]
        resolved_from_session["region"] = True

    if merged.get("time_window_minutes") is None and session_context.get("last_time_window_minutes") is not None:
        merged["time_window_minutes"] = session_context["last_time_window_minutes"]
        resolved_from_session["time_window_minutes"] = True

    if not merged.get("action_target") and merged.get("service_name"):
        merged["action_target"] = merged["service_name"]

    return merged, resolved_from_session


def _resolved_entities_payload(intent: str, entities: dict, resolved_from_session: dict) -> dict:
    return {
        "intent": intent,
        "service_name": entities.get("service_name"),
        "action_target": entities.get("action_target"),
        "version": entities.get("version"),
        "env": entities.get("env"),
        "namespace": entities.get("namespace"),
        "cluster": entities.get("cluster"),
        "region": entities.get("region"),
        "time_window_minutes": entities.get("time_window_minutes"),
        "resolved_from_session": resolved_from_session,
    }


def run_agent(
    message: str,
    confirm: bool = False,
    pending_action: dict | None = None,
    session_context: dict | None = None,
) -> dict:
    entities = extract_entities(message)
    entities, resolved_from_session = _merge_session_entities(entities, session_context)
    if not entities.get("service_name"):
        service_from_option = _resolve_service_from_pending_options(message, session_context)
        if service_from_option:
            entities["service_name"] = service_from_option
            entities["action_target"] = service_from_option
    intent = entities.get("intent") or detect_intent(message)
    pending_intent = (session_context or {}).get("pending_intent")
    if pending_intent and _looks_like_clarification_reply(message, entities, session_context):
        intent = pending_intent
    service_name = entities.get("service_name") or entities.get("action_target")
    version = entities.get("version")

    steps = []

    if intent == "deploy":
        if not service_name:
            service_options = _candidate_service_options(intent)
            return _build_clarification_response(
                intent=intent,
                entities=entities,
                resolved_from_session=resolved_from_session,
                missing_fields=["service_name"],
                question="要部署哪个服务？你可以直接回复服务名，例如 payment-service。",
                options=service_options or None,
            )

        if not version:
            return _build_clarification_response(
                intent=intent,
                entities=entities,
                resolved_from_session=resolved_from_session,
                missing_fields=["version"],
                question=f"准备部署 {service_name}，还差目标版本。你可以直接回复类似 v1.2.3。",
            )

        current = get_service_status(service_name)
        if not current:
            return {
                "intent": intent,
                "steps": [],
                "final_answer": f"没有找到服务 {service_name}。",
                "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
            }

        steps.append({
            "step": 1,
            "action": "get_service_status",
            "result": current
        })

        deploy_result = deploy_service(service_name, version)
        steps.append({
            "step": 2,
            "action": "deploy_service",
            "result": deploy_result
        })

        latest = get_service_status(service_name)
        steps.append({
            "step": 3,
            "action": "get_service_status",
            "result": latest
        })

        if latest["status"] == "running":
            final_answer = (
                f"{service_name} 已部署到 {version}，当前状态为 running。"
                f" 原版本是 {current['version']}，当前错误率 {latest['error_rate']}%。"
            )
        else:
            final_answer = (
                f"{service_name} 已尝试部署到 {version}，但当前状态为 {latest['status']}。"
                f" 错误率为 {latest['error_rate']}%，建议继续排查或回滚。"
            )

        final_answer, generation_meta = _summarize_with_llm(message, intent, steps, final_answer)

        return {
            "intent": intent,
            "steps": steps,
            "final_answer": final_answer,
            "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
            **generation_meta,
        }

    if intent == "rollback":
        if not service_name:
            service_options = _candidate_service_options(intent)
            return _build_clarification_response(
                intent=intent,
                entities=entities,
                resolved_from_session=resolved_from_session,
                missing_fields=["service_name"],
                question="要回滚哪个服务？你可以直接回复服务名。",
                options=service_options or None,
            )

        current = get_service_status(service_name)
        if not current:
            return {
                "intent": intent,
                "steps": [],
                "final_answer": f"没有找到服务 {service_name}。",
                "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
            }

        steps.append({
            "step": 1,
            "action": "get_service_status",
            "result": current
        })

        policy_decision = evaluate_action_policy("rollback", service_name)
        steps.append({
            "step": 2,
            "action": "evaluate_action_policy",
            "result": policy_decision,
        })

        return {
            "intent": intent,
            "steps": steps,
            "final_answer": (
                f"检测到高危操作：准备回滚 {service_name}。"
                f" {policy_decision['summary']} 请先确认后再执行。"
            ),
            "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
            "policy_decision": policy_decision,
            "execution_mode": "pending_confirmation",
            "requires_confirmation": policy_decision.get("requires_confirmation", True),
            "pending_action": {
                "action_type": "rollback",
                "service_name": service_name,
                "policy_decision": policy_decision,
                "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
            },
        }

    if intent == "troubleshoot":
        if not service_name:
            alerts = get_recent_alerts(limit=5)
            steps.append({
                "step": 1,
                "action": "get_recent_alerts",
                "result": alerts
            })

            if not alerts:
                service_options = _candidate_service_options(intent)
                return {
                    **_build_clarification_response(
                        intent=intent,
                        entities=entities,
                        resolved_from_session=resolved_from_session,
                        missing_fields=["service_name"],
                        question="当前没有明确告警上下文，请告诉我要排查哪个服务。",
                        options=service_options or None,
                    ),
                    "steps": steps,
                }

            service_name = alerts[0]["service"]
            entities["service_name"] = service_name

        alerts = get_recent_alerts(service_name=service_name, limit=5)
        steps.append({
            "step": 1,
            "action": "get_recent_alerts",
            "result": alerts
        })

        status = get_service_status(service_name)
        steps.append({
            "step": 2,
            "action": "get_service_status",
            "result": status
        })

        metrics = get_service_metrics(service_name)
        steps.append({
            "step": 3,
            "action": "get_service_metrics",
            "result": metrics
        })

        logs = get_recent_logs(service_name, limit=5)
        steps.append({
            "step": 4,
            "action": "get_recent_logs",
            "result": logs
        })

        recent_changes = get_recent_deploy_context(service_name, limit=3)
        steps.append({
            "step": 5,
            "action": "get_recent_deploy_context",
            "result": recent_changes
        })

        k8s_observability = get_external_k8s_observability(
            service_name,
            namespace=entities.get("namespace"),
        )
        steps.append({
            "step": 6,
            "action": "get_k8s_observability",
            "result": k8s_observability or {},
        })

        fallback_assessment = _build_fallback_troubleshoot_assessment(
            service_name=service_name,
            alerts=alerts,
            status=status,
            metrics=metrics,
            logs=logs,
            recent_changes=recent_changes,
            k8s_observability=k8s_observability,
        )

        assessment, generation_meta = generate_troubleshoot_assessment(
            user_message=message,
            service_name=service_name,
            alerts=alerts,
            status=status,
            metrics=metrics,
            logs=logs,
            recent_changes=recent_changes,
            fallback=fallback_assessment,
        )

        final_answer = _format_troubleshoot_final_answer(service_name, assessment)

        return {
            "intent": intent,
            "steps": steps,
            "final_answer": final_answer,
            "assessment_details": assessment,
            "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
            **generation_meta,
        }

    if intent == "status_query":
        if service_name:
            status = get_service_status(service_name)
            if not status:
                return {
                    "intent": intent,
                    "steps": [],
                    "final_answer": f"没有找到服务 {service_name}。",
                    "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
                }

            steps.append({
                "step": 1,
                "action": "get_service_status",
                "result": status
            })

            final_answer = (
                f"{service_name} 当前版本 {status['version']}，状态 {status['status']}，"
                f"CPU {status['cpu']}%，内存 {status['memory']}%，错误率 {status['error_rate']}%。"
            )
            if status.get("base_url"):
                final_answer += f" 目标地址 {status['base_url']}。"
            if status.get("latency_ms") is not None:
                final_answer += f" 最近延迟 {status['latency_ms']}ms。"
            final_answer, generation_meta = _summarize_with_llm(message, intent, steps, final_answer)
            return {
                "intent": intent,
                "steps": steps,
                "final_answer": final_answer,
                "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
                **generation_meta,
            }

        services = list_services()
        steps.append({
            "step": 1,
            "action": "list_services",
            "result": services
        })

        summary = "；".join(
            f"{svc['name']}={svc['status']}({svc['version']})"
            for svc in services
        )
        final_answer = f"当前服务概览：{summary}"
        final_answer, generation_meta = _summarize_with_llm(message, intent, steps, final_answer)
        return {
            "intent": intent,
            "steps": steps,
            "final_answer": final_answer,
            "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
            **generation_meta,
        }

    return {
        "intent": intent,
        "steps": [],
        "final_answer": "我暂时无法识别这个运维任务。你可以试试：部署 payment-service 到 v1.2.3。",
        "resolved_entities": _resolved_entities_payload(intent, entities, resolved_from_session),
    }


def execute_confirmed_action(pending_action: dict) -> dict:
    action_type = pending_action.get("action_type")
    service_name = pending_action.get("service_name")
    dry_run = bool(pending_action.get("dry_run"))
    steps = []

    if action_type != "rollback":
        return {
            "intent": "unknown",
            "steps": [],
            "final_answer": f"暂不支持执行该确认操作：{action_type}",
            "requires_confirmation": False,
            "pending_action": None,
        }

    if not service_name:
        return {
            "intent": "rollback",
            "steps": [],
            "final_answer": "待确认操作缺少服务名，无法执行回滚。",
            "execution_mode": "dry_run" if dry_run else "execute",
            "requires_confirmation": False,
            "pending_action": None,
        }

    current = get_service_status(service_name)
    if not current:
        return {
            "intent": "rollback",
            "steps": [],
            "final_answer": f"没有找到服务 {service_name}。",
            "execution_mode": "dry_run" if dry_run else "execute",
            "requires_confirmation": False,
            "pending_action": None,
        }

    steps.append({
        "step": 1,
        "action": "get_service_status",
        "result": current
    })

    policy_decision = evaluate_action_policy(action_type, service_name)
    steps.append({
        "step": 2,
        "action": "evaluate_action_policy",
        "result": policy_decision,
    })

    if not policy_decision.get("allowed"):
        return {
            "intent": "rollback",
            "steps": steps,
            "final_answer": policy_decision["summary"],
            "policy_decision": policy_decision,
            "execution_mode": "denied",
            "requires_confirmation": False,
            "pending_action": None,
        }

    if dry_run:
        preview = build_execution_preview(action_type, service_name)
        steps.append({
            "step": 3,
            "action": "build_execution_preview",
            "result": preview,
        })
        return {
            "intent": "rollback",
            "steps": steps,
            "final_answer": (
                f"已完成 dry-run。{preview['message']}"
                f" 预计执行步骤：{'；'.join(preview['preview_steps'])}"
            ),
            "policy_decision": policy_decision,
            "execution_mode": "dry_run",
            "requires_confirmation": False,
            "pending_action": None,
        }

    rollback_result = rollback_service(service_name)
    steps.append({
        "step": 3,
        "action": "rollback_service",
        "result": rollback_result
    })

    latest = get_service_status(service_name)
    steps.append({
        "step": 4,
        "action": "get_service_status",
        "result": latest
    })

    final_answer = (
        f"{service_name} 已完成回滚，当前版本为 {latest['version']}，"
        f"状态为 {latest['status']}，当前错误率 {latest['error_rate']}%。"
    )

    final_answer, generation_meta = _summarize_with_llm(f"确认执行回滚 {service_name}", "rollback", steps, final_answer)

    return {
        "intent": "rollback",
        "steps": steps,
        "final_answer": final_answer,
        "policy_decision": policy_decision,
        "execution_mode": "execute",
        **generation_meta,
        "requires_confirmation": False,
        "pending_action": None,
    }
