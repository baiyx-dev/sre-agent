from fastapi import APIRouter, Depends

from backend.security_auth import require_viewer
from fastapi.responses import PlainTextResponse

from backend.services.observability_service import build_metrics_snapshot

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_viewer)])


@router.get("/metrics")
def internal_metrics():
    return {
        "service": "sre-agent",
        "metrics": build_metrics_snapshot(),
    }


@router.get("/prometheus", response_class=PlainTextResponse)
def prometheus_metrics():
    metrics = build_metrics_snapshot()
    queue = metrics["change_queue"]
    workers = metrics["change_workers"]
    incidents = metrics["incidents"]
    commercial_usage = metrics["commercial_usage"]
    lines = [
        "# HELP sre_agent_request_total Total number of handled HTTP requests.",
        "# TYPE sre_agent_request_total counter",
        f"sre_agent_request_total {metrics['request_count']}",
        "# HELP sre_agent_success_rate_pct Successful request ratio in percentage.",
        "# TYPE sre_agent_success_rate_pct gauge",
        f"sre_agent_success_rate_pct {metrics['success_rate_pct']}",
        "# HELP sre_agent_avg_response_time_ms Average response time in milliseconds.",
        "# TYPE sre_agent_avg_response_time_ms gauge",
        f"sre_agent_avg_response_time_ms {metrics['avg_response_time_ms']}",
        "# HELP sre_agent_p95_response_time_ms P95 response time in milliseconds.",
        "# TYPE sre_agent_p95_response_time_ms gauge",
        f"sre_agent_p95_response_time_ms {metrics['p95_response_time_ms']}",
        "# HELP sre_agent_usage_metering_failures_total Failed durable usage metering writes.",
        "# TYPE sre_agent_usage_metering_failures_total counter",
        f"sre_agent_usage_metering_failures_total {metrics['usage_metering_failures']}",
        "# HELP sre_agent_change_jobs Number of durable change jobs by status.",
        "# TYPE sre_agent_change_jobs gauge",
        f'sre_agent_change_jobs{{status="queued"}} {queue["queued"]}',
        f'sre_agent_change_jobs{{status="running"}} {queue["running"]}',
        f'sre_agent_change_jobs{{status="succeeded"}} {queue["succeeded"]}',
        f'sre_agent_change_jobs{{status="failed"}} {queue["failed"]}',
        f'sre_agent_change_jobs{{status="unknown"}} {queue["unknown"]}',
        f'sre_agent_change_jobs{{status="cancelled"}} {queue["cancelled"]}',
        "# HELP sre_agent_change_workers Number of durable change workers by heartbeat state.",
        "# TYPE sre_agent_change_workers gauge",
        f'sre_agent_change_workers{{state="active"}} {workers["active_count"]}',
        f'sre_agent_change_workers{{state="stale"}} {workers["stale_count"]}',
        "# HELP sre_agent_incidents Number of incidents by lifecycle status.",
        "# TYPE sre_agent_incidents gauge",
        f'sre_agent_incidents{{status="open"}} {incidents["open"]}',
        f'sre_agent_incidents{{status="investigating"}} {incidents["investigating"]}',
        f'sre_agent_incidents{{status="mitigated"}} {incidents["mitigated"]}',
        f'sre_agent_incidents{{status="resolved"}} {incidents["resolved"]}',
        "# HELP sre_agent_monthly_requests_used Metered workspace API requests in the current UTC month.",
        "# TYPE sre_agent_monthly_requests_used gauge",
        f'sre_agent_monthly_requests_used{{plan="{commercial_usage["plan"]}"}} {commercial_usage["requests_used"]}',
        "# HELP sre_agent_monthly_request_limit Configured monthly request limit; zero means unlimited.",
        "# TYPE sre_agent_monthly_request_limit gauge",
        f'sre_agent_monthly_request_limit{{plan="{commercial_usage["plan"]}"}} {commercial_usage["monthly_request_limit"]}',
        "# HELP sre_agent_llm_cost_usd_micros Estimated LLM cost in USD micros for the current UTC month.",
        "# TYPE sre_agent_llm_cost_usd_micros gauge",
        f'sre_agent_llm_cost_usd_micros{{plan="{commercial_usage["plan"]}"}} {commercial_usage["llm_cost_usd_micros"]}',
    ]
    return "\n".join(lines) + "\n"
