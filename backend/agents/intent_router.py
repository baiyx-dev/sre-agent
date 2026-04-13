import re
from urllib.parse import urlparse

from backend.llm.provider import classify_intent_with_llm, extract_entities_with_llm
from backend.tools.service_tool import list_services


def _rule_detect_intent(message: str) -> str:
    text = message.lower().strip()

    if any(word in text for word in ["回滚", "rollback", "撤回"]):
        return "rollback"

    if any(word in text for word in ["部署", "发布", "deploy"]):
        return "deploy"

    if any(word in text for word in ["报警", "报错", "故障", "异常", "排查", "看看", "检查"]):
        return "troubleshoot"

    if any(word in text for word in ["状态", "service", "服务", "metrics", "日志", "告警", "启动", "存活", "健康"]):
        return "status_query"

    return "unknown"


def detect_intent(message: str) -> str:
    rule_intent = _rule_detect_intent(message)

    # 强动作意图优先走确定性规则，避免误判到其他类型。
    if rule_intent in ("deploy", "rollback"):
        return rule_intent

    llm_intent = classify_intent_with_llm(message)
    if llm_intent and llm_intent != "unknown":
        return llm_intent

    return rule_intent


def extract_entities(message: str) -> dict:
    services = list_services()
    rule_intent = _rule_detect_intent(message)
    rule_service_name = _rule_extract_service_name(message, services)
    rule_version = extract_version(message)
    rule_env = extract_env(message)
    rule_namespace = extract_namespace(message)
    rule_cluster = extract_cluster(message)
    rule_region = extract_region(message)
    rule_time_window_minutes = extract_time_window_minutes(message)

    llm_entities = extract_entities_with_llm(message, services)

    intent = None
    if rule_intent in ("deploy", "rollback"):
        intent = rule_intent
    else:
        llm_intent = llm_entities.get("intent") if isinstance(llm_entities, dict) else None
        intent = llm_intent if llm_intent and llm_intent != "unknown" else detect_intent(message)

    service_name = rule_service_name
    if not service_name and isinstance(llm_entities, dict):
        service_name = llm_entities.get("service_name") or llm_entities.get("action_target")

    action_target = service_name
    if isinstance(llm_entities, dict) and llm_entities.get("action_target"):
        action_target = llm_entities.get("action_target")

    env = rule_env or (llm_entities.get("env") if isinstance(llm_entities, dict) else None)
    namespace = rule_namespace or (llm_entities.get("namespace") if isinstance(llm_entities, dict) else None)
    cluster = rule_cluster or (llm_entities.get("cluster") if isinstance(llm_entities, dict) else None)
    region = rule_region or (llm_entities.get("region") if isinstance(llm_entities, dict) else None)
    version = rule_version or (llm_entities.get("version") if isinstance(llm_entities, dict) else None)
    time_window_minutes = rule_time_window_minutes
    if time_window_minutes is None and isinstance(llm_entities, dict):
        time_window_minutes = llm_entities.get("time_window_minutes")

    return {
        "intent": intent or "unknown",
        "service_name": service_name,
        "action_target": action_target,
        "env": env,
        "namespace": namespace,
        "cluster": cluster,
        "region": region,
        "version": version,
        "time_window_minutes": time_window_minutes,
    }


def extract_service_name(message: str) -> str | None:
    services = list_services()
    return _rule_extract_service_name(message, services)


def _rule_extract_service_name(message: str, services: list[dict]) -> str | None:
    text = message.lower()
    candidates = []
    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = (svc.get("name") or "").lower()
        base_url = (svc.get("base_url") or "").lower()
        host = ""
        if base_url:
            try:
                host = (urlparse(base_url).netloc or "").lower()
            except Exception:
                host = ""
        if name:
            candidates.append((name, name))
        if base_url:
            candidates.append((base_url, name))
        if host:
            candidates.append((host, name))

    candidates.sort(key=lambda x: len(x[0]), reverse=True)
    for keyword, name in candidates:
        if keyword and keyword in text:
            return name
    return None


def extract_version(message: str) -> str | None:
    match = re.search(r"v\d+(?:\.\d+){0,2}", message.lower())
    if match:
        return match.group(0)
    return None


def extract_env(message: str) -> str | None:
    match = re.search(r"\b(prod|production|staging|stage|test|dev|qa)\b", message.lower())
    if not match:
        return None
    env = match.group(1)
    if env == "production":
        return "prod"
    if env == "stage":
        return "staging"
    return env


def extract_namespace(message: str) -> str | None:
    match = re.search(r"(?:namespace|ns)[=:：\s-]*([a-z0-9-]+)", message.lower())
    return match.group(1) if match else None


def extract_cluster(message: str) -> str | None:
    match = re.search(r"(?:cluster|集群)[=:：\s-]*([a-z0-9-]+)", message.lower())
    return match.group(1) if match else None


def extract_region(message: str) -> str | None:
    match = re.search(r"\b(ap|cn|eu|us)[-_][a-z0-9-]+\b", message.lower())
    return match.group(0) if match else None


def extract_time_window_minutes(message: str) -> int | None:
    lowered = message.lower()
    hour_match = re.search(r"(最近|过去)?\s*(\d+)\s*小时", lowered)
    if hour_match:
        return int(hour_match.group(2)) * 60
    minute_match = re.search(r"(最近|过去)?\s*(\d+)\s*分钟", lowered)
    if minute_match:
        return int(minute_match.group(2))
    return None
