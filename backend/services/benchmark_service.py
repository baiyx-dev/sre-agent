from copy import deepcopy
from contextlib import contextmanager

from backend.agents.orchestrator import run_agent
from backend.storage.seed import reset_seed_data
from backend.tools import alert_tool, logs_tool, metrics_tool, service_tool


BENCHMARK_SCENARIOS = [
    {
        "id": "payment_troubleshoot",
        "title": "Payment service timeout investigation",
        "message": "payment-service 报警了，帮我排查",
        "expected": {
            "intent": "troubleshoot",
            "severity": "high",
            "hypothesis_keywords": ["数据库", "依赖", "变更", "实例"],
            "next_action_keywords": ["检查", "核对", "确认"],
            "evidence_keywords": ["error rate", "timeout", "告警"],
        },
    },
    {
        "id": "payment_troubleshoot_natural_language",
        "title": "Natural language troubleshooting request",
        "message": "帮我看看 payment-service 最近是不是有问题",
        "expected": {
            "intent": "troubleshoot",
            "severity": "high",
            "hypothesis_keywords": ["数据库", "依赖", "实例"],
            "next_action_keywords": ["检查", "确认"],
            "evidence_keywords": ["error rate", "timeout", "告警"],
        },
    },
    {
        "id": "payment_status",
        "title": "Payment service health snapshot",
        "message": "payment-service 状态",
        "expected": {
            "intent": "status_query",
            "answer_keywords": ["payment-service", "状态"],
        },
    },
    {
        "id": "order_status",
        "title": "Healthy service status snapshot",
        "message": "order-service 状态",
        "expected": {
            "intent": "status_query",
            "answer_keywords": ["order-service", "running"],
        },
    },
    {
        "id": "payment_rollback_confirmation",
        "title": "Rollback requires confirmation",
        "message": "回滚 payment-service",
        "expected": {
            "intent": "rollback",
            "requires_confirmation": True,
            "answer_keywords": ["确认", "回滚"],
            "policy_recommended_mode": "dry_run",
        },
    },
    {
        "id": "payment_deploy_clarification",
        "title": "Deploy requires target version clarification",
        "message": "部署 payment-service",
        "expected": {
            "intent": "deploy",
            "requires_clarification": True,
            "clarification_question_keywords": ["目标版本"],
        },
    },
]


def list_benchmark_scenarios() -> list[dict]:
    scenarios = []
    for item in BENCHMARK_SCENARIOS:
        scenarios.append({
            "id": item["id"],
            "title": item["title"],
            "message": item["message"],
            "expected": deepcopy(item["expected"]),
        })
    return scenarios


@contextmanager
def _isolated_benchmark_mode():
    original_functions = {
        "service_tool.get_external_services": service_tool.get_external_services,
        "service_tool.get_external_service_status": service_tool.get_external_service_status,
        "metrics_tool.get_external_metrics": metrics_tool.get_external_metrics,
        "logs_tool.get_external_logs": logs_tool.get_external_logs,
        "alert_tool.get_external_alerts": alert_tool.get_external_alerts,
    }

    service_tool.get_external_services = lambda: None
    service_tool.get_external_service_status = lambda service_name: None
    metrics_tool.get_external_metrics = lambda service_name: None
    logs_tool.get_external_logs = lambda service_name, limit=10: None
    alert_tool.get_external_alerts = lambda service_name=None, unresolved_only=True, limit=10: None

    try:
        yield
    finally:
        service_tool.get_external_services = original_functions["service_tool.get_external_services"]
        service_tool.get_external_service_status = original_functions["service_tool.get_external_service_status"]
        metrics_tool.get_external_metrics = original_functions["metrics_tool.get_external_metrics"]
        logs_tool.get_external_logs = original_functions["logs_tool.get_external_logs"]
        alert_tool.get_external_alerts = original_functions["alert_tool.get_external_alerts"]


def run_replay_scenario(scenario_id: str) -> dict | None:
    scenario = next((item for item in BENCHMARK_SCENARIOS if item["id"] == scenario_id), None)
    if not scenario:
        return None

    with _isolated_benchmark_mode():
        reset_seed_data()
        result = run_agent(scenario["message"])
    evaluation = _evaluate_result(result, scenario["expected"])
    return {
        "scenario": {
            "id": scenario["id"],
            "title": scenario["title"],
            "message": scenario["message"],
        },
        "expected": deepcopy(scenario["expected"]),
        "result": result,
        "evaluation": evaluation,
    }


def run_benchmark() -> dict:
    replays = [run_replay_scenario(item["id"]) for item in BENCHMARK_SCENARIOS]
    replays = [item for item in replays if item]

    total_score = sum(item["evaluation"]["score"] for item in replays)
    max_score = sum(item["evaluation"]["max_score"] for item in replays)
    passed = sum(1 for item in replays if item["evaluation"]["passed"])

    return {
        "summary": _summarize_benchmark(replays, total_score, max_score, passed),
        "replays": replays,
    }


def _evaluate_result(result: dict, expected: dict) -> dict:
    checks = []
    score = 0

    intent_match = result.get("intent") == expected.get("intent")
    checks.append({
        "name": "intent_match",
        "passed": intent_match,
        "details": f"expected={expected.get('intent')} actual={result.get('intent')}",
    })
    score += 1 if intent_match else 0

    requires_confirmation_expected = expected.get("requires_confirmation")
    requires_confirmation_match = True
    if requires_confirmation_expected is not None:
        requires_confirmation_match = result.get("requires_confirmation") == requires_confirmation_expected
        checks.append({
            "name": "confirmation_match",
            "passed": requires_confirmation_match,
            "details": (
                f"expected={requires_confirmation_expected} "
                f"actual={result.get('requires_confirmation')}"
            ),
        })
        score += 1 if requires_confirmation_match else 0

    requires_clarification_expected = expected.get("requires_clarification")
    clarification_match = True
    if requires_clarification_expected is not None:
        clarification_match = result.get("requires_clarification") == requires_clarification_expected
        checks.append({
            "name": "clarification_match",
            "passed": clarification_match,
            "details": (
                f"expected={requires_clarification_expected} "
                f"actual={result.get('requires_clarification')}"
            ),
        })
        score += 1 if clarification_match else 0

    severity_expected = expected.get("severity")
    severity_match = False
    if severity_expected:
        severity_actual = ((result.get("assessment_details") or {}).get("severity_assessment"))
        severity_match = severity_actual == severity_expected
        checks.append({
            "name": "severity_match",
            "passed": severity_match,
            "details": f"expected={severity_expected} actual={severity_actual}",
        })
        score += 1 if severity_match else 0

    answer_keywords = expected.get("answer_keywords") or []
    answer_keyword_hit = True
    if answer_keywords:
        answer_text = str(result.get("final_answer", ""))
        answer_keyword_hit = all(keyword in answer_text for keyword in answer_keywords)
        checks.append({
            "name": "answer_keywords",
            "passed": answer_keyword_hit,
            "details": f"required={answer_keywords}",
        })
        score += 1 if answer_keyword_hit else 0

    clarification_question_keywords = expected.get("clarification_question_keywords") or []
    clarification_question_hit = True
    if clarification_question_keywords:
        clarification_question = str(result.get("clarification_question", ""))
        clarification_question_hit = all(
            keyword in clarification_question for keyword in clarification_question_keywords
        )
        checks.append({
            "name": "clarification_question_keywords",
            "passed": clarification_question_hit,
            "details": f"required={clarification_question_keywords}",
        })
        score += 1 if clarification_question_hit else 0

    hypothesis_keywords = expected.get("hypothesis_keywords") or []
    hypothesis_hit = True
    if hypothesis_keywords:
        hypotheses = (result.get("assessment_details") or {}).get("hypotheses") or []
        joined_hypotheses = " ".join(
            f"{item.get('hypothesis', '')} {item.get('rationale', '')}"
            for item in hypotheses
            if isinstance(item, dict)
        )
        hypothesis_hit = any(keyword in joined_hypotheses for keyword in hypothesis_keywords)
        checks.append({
            "name": "hypothesis_keywords",
            "passed": hypothesis_hit,
            "details": f"keywords={hypothesis_keywords}",
        })
        score += 1 if hypothesis_hit else 0

    evidence_keywords = expected.get("evidence_keywords") or []
    evidence_hit = True
    if evidence_keywords:
        evidence_items = (result.get("assessment_details") or {}).get("evidence") or []
        joined_evidence = " ".join(str(item) for item in evidence_items)
        evidence_hit = any(keyword in joined_evidence for keyword in evidence_keywords)
        checks.append({
            "name": "evidence_keywords",
            "passed": evidence_hit,
            "details": f"keywords={evidence_keywords}",
        })
        score += 1 if evidence_hit else 0

    next_action_keywords = expected.get("next_action_keywords") or []
    next_action_hit = True
    if next_action_keywords:
        next_actions = (result.get("assessment_details") or {}).get("next_actions") or []
        joined_actions = " ".join(str(item) for item in next_actions)
        next_action_hit = any(keyword in joined_actions for keyword in next_action_keywords)
        checks.append({
            "name": "next_action_keywords",
            "passed": next_action_hit,
            "details": f"keywords={next_action_keywords}",
        })
        score += 1 if next_action_hit else 0

    policy_recommended_mode_expected = expected.get("policy_recommended_mode")
    policy_mode_match = True
    if policy_recommended_mode_expected:
        policy_mode_actual = ((result.get("policy_decision") or {}).get("recommended_mode"))
        policy_mode_match = policy_mode_actual == policy_recommended_mode_expected
        checks.append({
            "name": "policy_recommended_mode",
            "passed": policy_mode_match,
            "details": f"expected={policy_recommended_mode_expected} actual={policy_mode_actual}",
        })
        score += 1 if policy_mode_match else 0

    max_score = len(checks)
    return {
        "score": score,
        "max_score": max_score,
        "passed": score == max_score if max_score else True,
        "checks": checks,
        "severity_match": severity_match,
        "clarification_match": clarification_match,
        "clarification_question_hit": clarification_question_hit,
        "hypothesis_hit": hypothesis_hit,
        "evidence_hit": evidence_hit,
        "next_action_hit": next_action_hit,
        "policy_mode_match": policy_mode_match,
    }


def _summarize_benchmark(replays: list[dict], total_score: int, max_score: int, passed: int) -> dict:
    scenario_count = len(replays)
    return {
        "scenario_count": scenario_count,
        "passed_scenarios": passed,
        "score": total_score,
        "max_score": max_score,
        "pass_rate": _rate(passed, scenario_count),
        "average_score_rate": _rate(total_score, max_score),
        "intent_accuracy": _count_metric(replays, "intent_match"),
        "confirmation_accuracy": _count_metric(replays, "confirmation_match"),
        "clarification_accuracy": _count_metric(replays, "clarification_match"),
        "clarification_question_hit_rate": _count_metric(replays, "clarification_question_keywords"),
        "severity_hit_rate": _count_metric(replays, "severity_match"),
        "answer_keyword_hit_rate": _count_metric(replays, "answer_keywords"),
        "hypothesis_hit_rate": _count_metric(replays, "hypothesis_keywords"),
        "evidence_hit_rate": _count_metric(replays, "evidence_keywords"),
        "next_action_hit_rate": _count_metric(replays, "next_action_keywords"),
        "policy_mode_hit_rate": _count_metric(replays, "policy_recommended_mode"),
    }


def _count_metric(replays: list[dict], check_name: str) -> float:
    relevant_checks = []
    for item in replays:
        for check in item["evaluation"].get("checks", []):
            if check.get("name") == check_name:
                relevant_checks.append(check.get("passed"))
    if not relevant_checks:
        return 0.0
    passed = sum(1 for item in relevant_checks if item)
    return _rate(passed, len(relevant_checks))


def _rate(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 2)
