import json
import os
from dataclasses import dataclass

from backend.storage.repositories import get_recent_deploy_context
from backend.tools.alert_tool import get_recent_alerts
from backend.tools.external_data_source import get_external_k8s_observability
from backend.tools.logs_tool import get_recent_logs
from backend.tools.metrics_tool import get_service_metrics
from backend.tools.service_tool import get_service_status


@dataclass(frozen=True)
class EvidenceBudget:
    max_items_per_source: int = 20
    max_text_chars: int = 4000
    max_total_bytes: int = 65536

    @classmethod
    def from_environment(cls) -> "EvidenceBudget":
        return cls(
            max_items_per_source=_bounded_int("SRE_EVIDENCE_MAX_ITEMS", 20, 1, 200),
            max_text_chars=_bounded_int("SRE_EVIDENCE_MAX_TEXT_CHARS", 4000, 100, 20000),
            max_total_bytes=_bounded_int("SRE_EVIDENCE_MAX_TOTAL_BYTES", 65536, 4096, 1048576),
        )


@dataclass(frozen=True)
class AnalyzerResult:
    evidence: dict
    steps: list[dict]
    budget: dict


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _sanitize(value, budget: EvidenceBudget, depth: int = 0):
    if depth >= 8:
        return "[depth limit reached]"
    if isinstance(value, str):
        if len(value) <= budget.max_text_chars:
            return value
        return value[: budget.max_text_chars] + "…[truncated]"
    if isinstance(value, list):
        items = [
            _sanitize(item, budget, depth + 1)
            for item in value[: budget.max_items_per_source]
        ]
        if len(value) > budget.max_items_per_source:
            items.append({"_truncated_items": len(value) - budget.max_items_per_source})
        return items
    if isinstance(value, dict):
        return {
            str(key)[:200]: _sanitize(item, budget, depth + 1)
            for key, item in list(value.items())[:200]
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize(str(value), budget, depth + 1)


class ServiceHealthAnalyzer:
    name = "service_health"

    def __init__(self, budget: EvidenceBudget | None = None):
        self.budget = budget or EvidenceBudget.from_environment()

    def run(self, service_name: str, *, namespace: str | None = None) -> AnalyzerResult:
        raw_evidence = {
            "alerts": get_recent_alerts(
                service_name=service_name,
                limit=min(50, self.budget.max_items_per_source),
            ),
            "status": get_service_status(service_name),
            "metrics": get_service_metrics(service_name),
            "logs": get_recent_logs(
                service_name,
                limit=min(50, self.budget.max_items_per_source),
            ),
            "recent_changes": get_recent_deploy_context(
                service_name,
                limit=min(20, self.budget.max_items_per_source),
            ),
            "k8s_observability": get_external_k8s_observability(
                service_name,
                namespace=namespace,
            ) or {},
        }
        evidence = {}
        truncated_sources = []
        # Keep deterministic health signals before lower-value verbose sources.
        for source in (
            "status",
            "metrics",
            "alerts",
            "recent_changes",
            "logs",
            "k8s_observability",
        ):
            sanitized = _sanitize(raw_evidence[source], self.budget)
            candidate = {**evidence, source: sanitized}
            encoded_size = len(
                json.dumps(candidate, ensure_ascii=False, default=str).encode("utf-8")
            )
            if encoded_size > self.budget.max_total_bytes:
                evidence[source] = {
                    "_truncated": True,
                    "reason": "total evidence byte budget exceeded",
                }
                truncated_sources.append(source)
            else:
                evidence[source] = sanitized

        total_bytes = len(json.dumps(evidence, ensure_ascii=False).encode("utf-8"))
        step_sources = (
            ("alerts", "get_recent_alerts"),
            ("status", "get_service_status"),
            ("metrics", "get_service_metrics"),
            ("logs", "get_recent_logs"),
            ("recent_changes", "get_recent_deploy_context"),
            ("k8s_observability", "get_k8s_observability"),
        )
        steps = [
            {"step": index, "action": action, "result": evidence[source]}
            for index, (source, action) in enumerate(step_sources, start=1)
        ]
        budget_metadata = {
            "max_items_per_source": self.budget.max_items_per_source,
            "max_text_chars": self.budget.max_text_chars,
            "max_total_bytes": self.budget.max_total_bytes,
            "actual_total_bytes": total_bytes,
            "truncated_sources": truncated_sources,
        }
        steps.append(
            {
                "step": len(steps) + 1,
                "action": "evidence_budget",
                "result": budget_metadata,
            }
        )
        return AnalyzerResult(evidence=evidence, steps=steps, budget=budget_metadata)
