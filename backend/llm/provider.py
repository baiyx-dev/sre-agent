import json
import os
from urllib import error, request

from dotenv import load_dotenv

load_dotenv()


def _request_chat_completion(messages: list, temperature: float = 0.2) -> dict:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return {
            "ok": False,
            "content": None,
            "generation_source": "fallback_no_api_key",
            "llm_provider": "deepseek",
            "used_fallback": True,
            "fallback_reason": "missing_api_key",
        }

    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    endpoint = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1").rstrip("/") + "/chat/completions"

    body = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
    }

    req = request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"].strip()
            if not content:
                return {
                    "ok": False,
                    "content": None,
                    "generation_source": "fallback_llm_error",
                    "llm_provider": "deepseek",
                    "used_fallback": True,
                    "fallback_reason": "invalid_response",
                }
            return {
                "ok": True,
                "content": content,
                "generation_source": "deepseek",
                "llm_provider": "deepseek",
                "used_fallback": False,
                "fallback_reason": None,
            }
    except TimeoutError:
        return {
            "ok": False,
            "content": None,
            "generation_source": "fallback_llm_error",
            "llm_provider": "deepseek",
            "used_fallback": True,
            "fallback_reason": "timeout",
        }
    except (error.HTTPError, error.URLError) as e:
        reason = "timeout" if "timed out" in str(e).lower() else "request_error"
        return {
            "ok": False,
            "content": None,
            "generation_source": "fallback_llm_error",
            "llm_provider": "deepseek",
            "used_fallback": True,
            "fallback_reason": reason,
        }
    except (KeyError, IndexError, json.JSONDecodeError):
        return {
            "ok": False,
            "content": None,
            "generation_source": "fallback_llm_error",
            "llm_provider": "deepseek",
            "used_fallback": True,
            "fallback_reason": "invalid_response",
        }


def generate_final_answer(
    user_message: str,
    intent: str,
    steps: list,
    key_status: dict,
    fallback_answer: str,
) -> tuple[str, dict]:
    system_prompt = (
        "你是资深 SRE 工程师。"
        "只基于给定结构化执行数据输出 final_answer。"
        "禁止编造事实，禁止补充未提供的指标。"
        "用简洁清晰中文，2-4 句，先给结论再给关键依据。"
    )

    user_payload = {
        "user_message": user_message,
        "intent": intent,
        "steps": steps,
        "key_status": key_status,
    }

    result = _request_chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "请根据以下 JSON 生成 final_answer：\\n" + json.dumps(user_payload, ensure_ascii=False),
            },
        ],
        temperature=0.2,
    )

    if result["ok"]:
        return result["content"], {
            "generation_source": result["generation_source"],
            "llm_provider": result["llm_provider"],
            "used_fallback": result["used_fallback"],
            "fallback_reason": result["fallback_reason"],
        }

    return fallback_answer, {
        "generation_source": result["generation_source"],
        "llm_provider": result["llm_provider"],
        "used_fallback": result["used_fallback"],
        "fallback_reason": result["fallback_reason"],
    }


def classify_intent_with_llm(user_message: str) -> str | None:
    system_prompt = (
        "你是 SRE Copilot 的意图分类器。"
        "请根据用户输入判断最合适的意图。"
        "可选值仅有：status_query、troubleshoot、deploy、rollback、unknown。"
        "只返回一个意图字符串，禁止输出其他内容。"
        "判断原则："
        "如果用户是在了解服务当前状态、健康度、是否正常、最近怎么样，输出 status_query；"
        "如果用户是在请求分析原因、帮忙排查、看为什么异常、看问题出在哪，输出 troubleshoot；"
        "如果用户是在请求发布或升级版本，输出 deploy；"
        "如果用户是在请求回退版本或撤销发布，输出 rollback。"
    )

    result = _request_chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0,
    )

    if not result["ok"] or not result["content"]:
        return None

    intent = result["content"].strip().lower()
    if intent in {"status_query", "troubleshoot", "deploy", "rollback", "unknown"}:
        return intent
    return None


def extract_entities_with_llm(user_message: str, known_services: list[dict]) -> dict | None:
    service_candidates = []
    for item in known_services[:30]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            service_candidates.append(name)

    system_prompt = (
        "你是 SRE Copilot 的实体抽取器。"
        "请根据用户输入抽取运维意图和关键实体。"
        "输出必须是 JSON，且仅包含 "
        "intent、service_name、action_target、env、namespace、cluster、region、version、time_window_minutes 九个字段。"
        "intent 可选值仅有：status_query、troubleshoot、deploy、rollback、unknown。"
        "如果没有识别到某个字段，值必须为 null。"
        "service_name 必须优先从给定候选服务名中选择最匹配的一项；如果都不匹配则返回 null。"
        "action_target 表示用户提到的操作目标名称；如果能确认是某个已知服务，也应该与 service_name 保持一致。"
        "time_window_minutes 必须是整数分钟，例如最近 30 分钟返回 30，最近 2 小时返回 120。"
        "不要输出额外解释。"
    )

    payload = {
        "user_message": user_message,
        "known_services": service_candidates,
    }

    result = _request_chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "请输出实体抽取 JSON：\n" + json.dumps(payload, ensure_ascii=False),
            },
        ],
        temperature=0,
    )

    if not result["ok"] or not result["content"]:
        return None

    try:
        parsed = json.loads(result["content"])
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    intent = parsed.get("intent")
    service_name = parsed.get("service_name")
    action_target = parsed.get("action_target")
    env = parsed.get("env")
    namespace = parsed.get("namespace")
    cluster = parsed.get("cluster")
    region = parsed.get("region")
    version = parsed.get("version")
    time_window_minutes = parsed.get("time_window_minutes")

    if intent not in {"status_query", "troubleshoot", "deploy", "rollback", "unknown", None}:
        intent = None
    if service_name is not None and service_name not in service_candidates:
        service_name = None
    if action_target is not None and not isinstance(action_target, str):
        action_target = None
    if env is not None and not isinstance(env, str):
        env = None
    if namespace is not None and not isinstance(namespace, str):
        namespace = None
    if cluster is not None and not isinstance(cluster, str):
        cluster = None
    if region is not None and not isinstance(region, str):
        region = None
    if version is not None and not isinstance(version, str):
        version = None
    if isinstance(time_window_minutes, str) and time_window_minutes.isdigit():
        time_window_minutes = int(time_window_minutes)
    if time_window_minutes is not None and not isinstance(time_window_minutes, int):
        time_window_minutes = None

    return {
        "intent": intent,
        "service_name": service_name,
        "action_target": action_target,
        "env": env,
        "namespace": namespace,
        "cluster": cluster,
        "region": region,
        "version": version,
        "time_window_minutes": time_window_minutes,
    }


def generate_troubleshoot_assessment(
    user_message: str,
    service_name: str,
    alerts: list,
    status: dict,
    metrics: dict,
    logs: list,
    recent_changes: list,
    fallback: dict,
) -> tuple[dict, dict]:
    system_prompt = (
        "你是资深 SRE 故障诊断工程师。"
        "仅根据输入数据进行诊断，不编造未出现的故障。"
        "优先依据日志、指标、告警与近期变更，给出工程化结论。"
        "输出必须是 JSON，且仅包含 "
        "summary/severity_assessment/confidence/evidence/hypotheses/missing_signals/next_actions 七个字段。"
        "其中 evidence、missing_signals、next_actions 为字符串数组，"
        "hypotheses 为对象数组，每个对象仅包含 hypothesis/confidence/rationale。"
    )

    payload = {
        "user_message": user_message,
        "service_name": service_name,
        "recent_alerts": alerts,
        "current_service_status": status,
        "metrics": metrics,
        "recent_logs": logs,
        "recent_deploy_or_rollback": recent_changes,
    }

    result = _request_chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "请根据以下 JSON 输出诊断 JSON：\\n" + json.dumps(payload, ensure_ascii=False),
            },
        ],
        temperature=0.1,
    )

    meta = {
        "generation_source": result["generation_source"],
        "llm_provider": result["llm_provider"],
        "used_fallback": result["used_fallback"],
        "fallback_reason": result["fallback_reason"],
    }

    if not result["ok"]:
        return fallback, meta

    try:
        parsed = json.loads(result["content"])
        summary = parsed.get("summary")
        severity = parsed.get("severity_assessment")
        confidence = parsed.get("confidence")
        evidence = parsed.get("evidence")
        hypotheses = parsed.get("hypotheses")
        missing_signals = parsed.get("missing_signals")
        next_actions = parsed.get("next_actions")
        if (
            not summary
            or not severity
            or confidence is None
            or not isinstance(evidence, list)
            or not isinstance(hypotheses, list)
            or not isinstance(missing_signals, list)
            or not isinstance(next_actions, list)
        ):
            return fallback, {
                "generation_source": "fallback_llm_error",
                "llm_provider": "deepseek",
                "used_fallback": True,
                "fallback_reason": "invalid_response",
            }

        normalized_hypotheses = []
        for item in hypotheses[:3]:
            if not isinstance(item, dict):
                continue
            hypothesis = item.get("hypothesis")
            hypothesis_confidence = item.get("confidence")
            rationale = item.get("rationale")
            if not hypothesis or hypothesis_confidence is None or not rationale:
                continue
            normalized_hypotheses.append({
                "hypothesis": str(hypothesis),
                "confidence": str(hypothesis_confidence),
                "rationale": str(rationale),
            })

        if not normalized_hypotheses:
            return fallback, {
                "generation_source": "fallback_llm_error",
                "llm_provider": "deepseek",
                "used_fallback": True,
                "fallback_reason": "invalid_response",
            }

        return {
            "summary": str(summary),
            "severity_assessment": str(severity),
            "confidence": str(confidence),
            "evidence": [str(item) for item in evidence[:5]],
            "hypotheses": normalized_hypotheses,
            "missing_signals": [str(item) for item in missing_signals[:5]],
            "next_actions": [str(item) for item in next_actions[:5]],
        }, meta
    except (json.JSONDecodeError, AttributeError):
        return fallback, {
            "generation_source": "fallback_llm_error",
            "llm_provider": "deepseek",
            "used_fallback": True,
            "fallback_reason": "invalid_response",
        }


def generate_postmortem_narrative(postmortem: dict, fallback_summary: str) -> str:
    system_prompt = (
        "你是资深 SRE 事故复盘工程师。"
        "只根据输入的结构化 postmortem 输出简洁专业复盘总结。"
        "不编造事实，必须覆盖事故现象、根因、处理动作、后续改进。"
        "输出 3-5 句中文。"
    )

    result = _request_chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "请根据以下 postmortem JSON 生成 narrative_summary：\n"
                + json.dumps(postmortem, ensure_ascii=False),
            },
        ],
        temperature=0.2,
    )
    return result["content"] if result["ok"] else fallback_summary
