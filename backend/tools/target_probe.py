from datetime import datetime
from urllib import error, request

from backend.storage.repositories import get_monitored_target, list_monitored_targets


def _probe_target(url: str) -> tuple[str, float | None, str | None]:
    req = request.Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with request.urlopen(req, timeout=5) as resp:
            status_code = getattr(resp, "status", 200)
            latency_ms = float(resp.headers.get("X-Response-Time-Ms", 0) or 0)
            if latency_ms <= 0:
                latency_ms = None
            # Reachable and not server error => running
            status = "running" if status_code < 500 else "degraded"
            return status, latency_ms, None
    except error.HTTPError as e:
        if e.code < 500:
            return "running", None, f"http_{e.code}"
        return "degraded", None, f"http_{e.code}"
    except error.URLError:
        return "down", None, "connection_error"
    except TimeoutError:
        return "down", None, "timeout"


def _service_from_target(target: dict) -> dict:
    status, latency_ms, probe_error = _probe_target(target["base_url"])
    consecutive_failures = 0 if status == "running" else 1
    success_rate = 100.0 if status == "running" else 0.0
    status_code_hint = 200 if status == "running" else 503
    return {
        "name": target["name"],
        "version": "unknown",
        "status": status,
        "cpu": 0.0,
        "memory": 0.0,
        "error_rate": 0.0 if status == "running" else 1.0,
        "replicas": 1,
        "last_deploy_time": target.get("created_at") or "",
        "base_url": target["base_url"],
        "latency_ms": latency_ms,
        "success_rate_5m": success_rate,
        "http_status_hint": status_code_hint,
        "consecutive_failures": consecutive_failures,
        "probe_error": probe_error,
    }


def get_target_services() -> list[dict]:
    targets = list_monitored_targets()
    return [_service_from_target(t) for t in targets]


def get_target_service_status(service_name: str) -> dict | None:
    target = get_monitored_target(service_name)
    if not target:
        return None
    return _service_from_target(target)


def get_target_metrics(service_name: str) -> dict | None:
    status = get_target_service_status(service_name)
    if not status:
        return None
    return {
        "service": status["name"],
        "cpu": status["cpu"],
        "memory": status["memory"],
        "error_rate": status["error_rate"],
        "replicas": status["replicas"],
        "status": status["status"],
        "latency_ms": status.get("latency_ms"),
        "success_rate_5m": status.get("success_rate_5m"),
        "consecutive_failures": status.get("consecutive_failures"),
    }


def get_target_logs(service_name: str, limit: int = 10) -> list[dict]:
    status = get_target_service_status(service_name)
    if not status:
        return []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entries = []

    if status["status"] == "running":
        entries.append({
            "service": service_name,
            "timestamp": now,
            "level": "INFO",
            "message": f"health probe succeeded status={status.get('http_status_hint')} latency_ms={status.get('latency_ms') or 'unknown'}",
        })
        entries.append({
            "service": service_name,
            "timestamp": now,
            "level": "INFO",
            "message": f"availability window healthy success_rate_5m={status.get('success_rate_5m')} consecutive_failures={status.get('consecutive_failures')}",
        })
    else:
        entries.append({
            "service": service_name,
            "timestamp": now,
            "level": "ERROR",
            "message": f"health probe failed status={status.get('status')} error={status.get('probe_error') or 'unknown'}",
        })
        entries.append({
            "service": service_name,
            "timestamp": now,
            "level": "WARN",
            "message": f"availability degraded success_rate_5m={status.get('success_rate_5m')} consecutive_failures={status.get('consecutive_failures')}",
        })

    return entries[:limit]


def get_target_alerts(service_name: str | None = None, unresolved_only: bool = True, limit: int = 10) -> list[dict]:
    if service_name:
        status = get_target_service_status(service_name)
        services = [status] if status else []
    else:
        services = get_target_services()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    alerts = []
    for svc in services:
        if service_name and svc["name"] != service_name:
            continue
        if svc["status"] == "running":
            continue
        alerts.append({
            "id": f"probe-{svc['name']}",
            "service": svc["name"],
            "severity": "critical" if svc["status"] == "down" else "warning",
            "title": "health check degraded",
            "message": f"probe status={svc['status']} http_status={svc.get('http_status_hint')} error={svc.get('probe_error')}",
            "created_at": now,
            "resolved": 0,
        })

    if unresolved_only:
        alerts = [a for a in alerts if a.get("resolved", 0) == 0]
    return alerts[:limit]
