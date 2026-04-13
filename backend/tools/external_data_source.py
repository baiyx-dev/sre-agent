import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib import error, parse, request

from dotenv import load_dotenv

from backend.storage.repositories import get_app_setting
from backend.tools.target_probe import (
    get_target_alerts,
    get_target_logs,
    get_target_metrics,
    get_target_service_status,
    get_target_services,
)

load_dotenv()


DEFAULT_PROM_QUERY_TEMPLATES = {
    "PROM_QUERY_UP": "sum(up{service_selector})",
    "PROM_QUERY_REPLICAS": "count(up{service_selector})",
    "PROM_QUERY_ERROR_RATE": '100 * sum(rate(http_requests_total{service_selector_with_status_5xx}[5m])) / clamp_min(sum(rate(http_requests_total{service_selector}[5m])), 0.001)',
    "PROM_QUERY_CPU": "100 * avg(rate(process_cpu_seconds_total{service_selector}[5m]))",
    "PROM_QUERY_MEMORY": "avg(process_resident_memory_bytes{service_selector}) / 1024 / 1024",
    "PROM_QUERY_LATENCY_P95_MS": "1000 * histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{service_selector}[5m])) by (le))",
    "PROM_ALERT_QUERY": 'ALERTS{alertstate="firing",service="{service_name}"}',
}

DEFAULT_LOKI_QUERY_TEMPLATE = '{{{label}="{service_name}"}}'


def _get_config(key: str, default: str | None = None) -> str | None:
    value = get_app_setting(key)
    if value is not None:
        return value
    return os.getenv(key, default)


def _normalize_service(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name") or item.get("service_name")
    if not name:
        return None
    return {
        "name": name,
        "version": item.get("version") or "unknown",
        "status": item.get("status") or "unknown",
        "cpu": float(item.get("cpu") or 0),
        "memory": float(item.get("memory") or 0),
        "error_rate": float(item.get("error_rate") or 0),
        "replicas": int(item.get("replicas") or 1),
        "last_deploy_time": item.get("last_deploy_time") or item.get("last_check_at") or "",
        "base_url": item.get("base_url"),
        "latency_ms": item.get("latency_ms"),
        "consecutive_failures": item.get("consecutive_failures"),
    }


def _normalize_metrics(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    service = item.get("service") or item.get("name") or item.get("service_name")
    if not service:
        return None
    return {
        "service": service,
        "cpu": float(item.get("cpu") or 0),
        "memory": float(item.get("memory") or 0),
        "error_rate": float(item.get("error_rate") or 0),
        "replicas": int(item.get("replicas") or 1),
        "status": item.get("status") or "unknown",
        "latency_ms": item.get("latency_ms"),
        "success_rate_5m": item.get("success_rate_5m"),
        "consecutive_failures": item.get("consecutive_failures"),
    }


def _normalize_alerts(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        alerts = payload.get("alerts")
        if isinstance(alerts, list):
            normalized = []
            for a in alerts:
                if not isinstance(a, dict):
                    continue
                normalized.append({
                    "id": a.get("id"),
                    "service": a.get("service") or a.get("service_name"),
                    "severity": a.get("severity") or "unknown",
                    "title": a.get("title") or a.get("type") or "alert",
                    "message": a.get("message") or "",
                    "created_at": a.get("created_at") or "",
                    "resolved": 1 if a.get("resolved") else 0,
                })
            return normalized
    return None


def _normalize_logs(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        logs = payload.get("logs")
        if isinstance(logs, list):
            service_name = payload.get("service_name")
            normalized = []
            for log in logs:
                if isinstance(log, str):
                    normalized.append({
                        "service": service_name,
                        "timestamp": "",
                        "level": "INFO",
                        "message": log,
                    })
                elif isinstance(log, dict):
                    normalized.append({
                        "service": log.get("service") or log.get("service_name") or service_name,
                        "timestamp": log.get("timestamp") or log.get("created_at") or "",
                        "level": log.get("level") or "INFO",
                        "message": log.get("message") or "",
                    })
            return normalized
    return None


def _request_json_absolute(base_url: str | None, path: str, query: dict | None = None, token: str | None = None):
    if not base_url:
        return None

    url = base_url.rstrip("/") + path
    if query:
        query = {k: v for k, v in query.items() if v is not None}
        if query:
            url += "?" + parse.urlencode(query)

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(url, headers=headers, method="GET")

    try:
        with request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None


def _request_json(path: str, query: dict | None = None):
    base_url = _get_config("SRE_DATA_API_BASE")
    token = _get_config("SRE_DATA_API_TOKEN")
    return _request_json_absolute(base_url, path, query=query, token=token)


def _request_k8s_json(path: str, query: dict | None = None):
    return _request_json_absolute(_k8s_base_url(), path, query=query, token=_k8s_token())


def _prometheus_base_url() -> str | None:
    return _get_config("PROMETHEUS_BASE_URL")


def _prometheus_token() -> str | None:
    return _get_config("PROMETHEUS_TOKEN")


def _loki_base_url() -> str | None:
    return _get_config("LOKI_BASE_URL")


def _loki_token() -> str | None:
    return _get_config("LOKI_TOKEN")


def _k8s_base_url() -> str | None:
    return _get_config("K8S_API_BASE")


def _k8s_token() -> str | None:
    return _get_config("K8S_API_TOKEN")


def _k8s_default_namespace() -> str:
    return (_get_config("K8S_NAMESPACE", "default") or "default").strip() or "default"


def _k8s_service_label() -> str:
    return (_get_config("K8S_SERVICE_LABEL", "app") or "app").strip() or "app"


def _service_label_candidates() -> list[str]:
    configured = (_get_config("PROMETHEUS_SERVICE_LABEL", "service") or "service").strip() or "service"
    loki_configured = (_get_config("LOKI_SERVICE_LABEL", configured) or configured).strip() or configured
    seen = []
    for item in [configured, loki_configured, "service", "job", "app", "application"]:
        if item and item not in seen:
            seen.append(item)
    return seen


def _get_template(key: str, default: str) -> str:
    value = (_get_config(key, default) or "").strip()
    return value or default


def _render_query_template(template: str, label: str, service_name: str) -> str:
    safe_service = service_name.replace("\\", "\\\\").replace('"', '\\"')
    service_selector = "{" + f'{label}="{safe_service}"' + "}"
    service_selector_with_status_5xx = "{" + f'{label}="{safe_service}",status=~"5.."' + "}"
    rendered = template
    rendered = rendered.replace("{service_selector_with_status_5xx}", service_selector_with_status_5xx)
    rendered = rendered.replace("{service_selector}", service_selector)
    rendered = rendered.replace("{service_name}", safe_service)
    rendered = rendered.replace("{label}", label)
    return rendered


def _prom_query(promql: str):
    payload = _request_json_absolute(
        _prometheus_base_url(),
        "/api/v1/query",
        query={"query": promql},
        token=_prometheus_token(),
    )
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    data = payload.get("data") or {}
    result = data.get("result")
    return result if isinstance(result, list) else None


def _prom_label_values(label: str) -> list[str]:
    payload = _request_json_absolute(
        _prometheus_base_url(),
        f"/api/v1/label/{parse.quote(label, safe='')}/values",
        token=_prometheus_token(),
    )
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def _extract_prom_value(result: list | None) -> float | None:
    if not result:
        return None
    first = result[0]
    if not isinstance(first, dict):
        return None
    value = first.get("value")
    if not isinstance(value, list) or len(value) < 2:
        return None
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None


def _selector(label: str, service_name: str) -> str:
    safe_service = service_name.replace("\\", "\\\\").replace('"', '\\"')
    return "{" + f'{label}="{safe_service}"' + "}"


def _discover_services_from_prometheus() -> tuple[str | None, list[str]]:
    for label in _service_label_candidates():
        values = _prom_label_values(label)
        values = [item for item in values if item not in ("", "prometheus", "loki")]
        if values:
            return label, sorted(set(values))
    return None, []


def _query_first_available(queries: list[str]) -> float | None:
    for promql in queries:
        value = _extract_prom_value(_prom_query(promql))
        if value is not None:
            return value
    return None


def _build_prom_metrics(service_name: str, label: str) -> dict | None:
    up_value = _query_first_available([
        _render_query_template(_get_template("PROM_QUERY_UP", DEFAULT_PROM_QUERY_TEMPLATES["PROM_QUERY_UP"]), label, service_name),
    ])
    target_count = _query_first_available([
        _render_query_template(_get_template("PROM_QUERY_REPLICAS", DEFAULT_PROM_QUERY_TEMPLATES["PROM_QUERY_REPLICAS"]), label, service_name),
    ])
    error_rate = _query_first_available([
        _render_query_template(_get_template("PROM_QUERY_ERROR_RATE", DEFAULT_PROM_QUERY_TEMPLATES["PROM_QUERY_ERROR_RATE"]), label, service_name),
        _render_query_template('100 * sum(rate(http_server_requests_seconds_count{service_selector_with_status_5xx}[5m])) / clamp_min(sum(rate(http_server_requests_seconds_count{service_selector}[5m])), 0.001)', label, service_name),
    ])
    cpu = _query_first_available([
        _render_query_template(_get_template("PROM_QUERY_CPU", DEFAULT_PROM_QUERY_TEMPLATES["PROM_QUERY_CPU"]), label, service_name),
        _render_query_template("100 * avg(rate(container_cpu_usage_seconds_total{service_selector}[5m]))", label, service_name),
    ])
    memory = _query_first_available([
        _render_query_template(_get_template("PROM_QUERY_MEMORY", DEFAULT_PROM_QUERY_TEMPLATES["PROM_QUERY_MEMORY"]), label, service_name),
        _render_query_template("avg(container_memory_working_set_bytes{service_selector}) / 1024 / 1024", label, service_name),
    ])
    latency_ms = _query_first_available([
        _render_query_template(_get_template("PROM_QUERY_LATENCY_P95_MS", DEFAULT_PROM_QUERY_TEMPLATES["PROM_QUERY_LATENCY_P95_MS"]), label, service_name),
        _render_query_template("1000 * histogram_quantile(0.95, sum(rate(http_server_requests_seconds_bucket{service_selector}[5m])) by (le))", label, service_name),
    ])

    if up_value is None and target_count is None and error_rate is None:
        return None

    replicas = int(round(target_count if target_count is not None else up_value if up_value is not None else 1))
    error_rate = float(error_rate or 0.0)
    consecutive_failures = 0 if (up_value or 0) > 0 else 1
    success_rate = max(0.0, 100.0 - error_rate)

    status = "unknown"
    if up_value is not None:
        if up_value <= 0:
            status = "down"
        elif error_rate > 5:
            status = "degraded"
        else:
            status = "running"

    return {
        "service": service_name,
        "cpu": round(float(cpu or 0.0), 2),
        "memory": round(float(memory or 0.0), 2),
        "error_rate": round(error_rate, 2),
        "replicas": max(replicas, 1),
        "status": status,
        "latency_ms": round(float(latency_ms), 2) if latency_ms is not None else None,
        "success_rate_5m": round(success_rate, 2),
        "consecutive_failures": consecutive_failures,
    }


def _get_prom_service_status(service_name: str) -> dict | None:
    label, services = _discover_services_from_prometheus()
    if not label or service_name not in services:
        return None
    metrics = _build_prom_metrics(service_name, label)
    if not metrics:
        return None
    return {
        "name": service_name,
        "version": "prometheus",
        "status": metrics["status"],
        "cpu": metrics["cpu"],
        "memory": metrics["memory"],
        "error_rate": metrics["error_rate"],
        "replicas": metrics["replicas"],
        "last_deploy_time": "",
        "latency_ms": metrics.get("latency_ms"),
        "consecutive_failures": metrics.get("consecutive_failures"),
    }


def _get_prom_services() -> list[dict] | None:
    label, services = _discover_services_from_prometheus()
    if not label or not services:
        return None
    normalized = []
    for service_name in services:
        status = _get_prom_service_status(service_name)
        if status:
            normalized.append(status)
    return normalized or None


def _get_prom_metrics(service_name: str) -> dict | None:
    label, services = _discover_services_from_prometheus()
    if not label or service_name not in services:
        return None
    return _build_prom_metrics(service_name, label)


def _prom_alerts(service_name: str | None = None, limit: int = 10) -> list[dict] | None:
    label_candidates = _service_label_candidates()
    if service_name:
        promql = _render_query_template(
            _get_template("PROM_ALERT_QUERY", DEFAULT_PROM_QUERY_TEMPLATES["PROM_ALERT_QUERY"]),
            label_candidates[0],
            service_name,
        )
    else:
        promql = 'ALERTS{alertstate="firing"}'
    result = _prom_query(promql)
    if result is None:
        return None

    alerts = []
    for item in result:
        metric = item.get("metric") or {}
        alert_service = None
        for label in label_candidates:
            if metric.get(label):
                alert_service = metric.get(label)
                break
        if service_name and alert_service and alert_service != service_name:
            continue
        timestamp = ""
        value = item.get("value")
        if isinstance(value, list) and value:
            try:
                timestamp = datetime.fromtimestamp(float(value[0]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                timestamp = ""
        alerts.append({
            "id": metric.get("alertname") or metric.get("__name__"),
            "service": alert_service,
            "severity": metric.get("severity") or "warning",
            "title": metric.get("alertname") or "prometheus_alert",
            "message": metric.get("summary") or metric.get("description") or "alert from Prometheus",
            "created_at": timestamp,
            "resolved": 0,
        })
    return alerts[:limit]


def _loki_query(service_name: str, limit: int = 10) -> list[dict] | None:
    base_url = _loki_base_url()
    if not base_url:
        return None

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=30)
    labels = _service_label_candidates()
    results = []

    for label in labels[:2]:
        query_string = _render_loki_query(label, service_name)
        payload = _request_json_absolute(
            base_url,
            "/loki/api/v1/query_range",
            query={
                "query": query_string,
                "limit": limit,
                "direction": "backward",
                "start": str(int(start.timestamp() * 1_000_000_000)),
                "end": str(int(end.timestamp() * 1_000_000_000)),
            },
            token=_loki_token(),
        )
        if not isinstance(payload, dict) or payload.get("status") != "success":
            continue
        data = payload.get("data") or {}
        streams = data.get("result")
        if not isinstance(streams, list):
            continue
        for stream in streams:
            values = stream.get("values") or []
            labels_data = stream.get("stream") or {}
            for value in values:
                if not isinstance(value, list) or len(value) < 2:
                    continue
                ts_raw, line = value[0], value[1]
                level_match = re.search(r"\b(INFO|WARN|WARNING|ERROR|DEBUG|CRITICAL)\b", str(line), re.IGNORECASE)
                level = level_match.group(1).upper() if level_match else "INFO"
                try:
                    timestamp = datetime.fromtimestamp(int(ts_raw) / 1_000_000_000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    timestamp = ""
                results.append({
                    "service": labels_data.get(label) or service_name,
                    "timestamp": timestamp,
                    "level": level,
                    "message": str(line),
                })
        if results:
            break

    return results[:limit] if results else None


def _render_loki_query(label: str, service_name: str) -> str:
    template = _get_template("LOKI_QUERY_TEMPLATE", DEFAULT_LOKI_QUERY_TEMPLATE)
    safe_service = service_name.replace("\\", "\\\\").replace('"', '\\"')
    return template.replace("{label}", label).replace("{service_name}", safe_service)


def _normalize_k8s_rollout(payload: dict, service_name: str, namespace: str) -> dict | None:
    if not isinstance(payload, dict):
        return None

    metadata = payload.get("metadata") or {}
    spec = payload.get("spec") or {}
    status = payload.get("status") or {}
    conditions = status.get("conditions") or []

    desired = int(spec.get("replicas") or 0)
    ready = int(status.get("readyReplicas") or 0)
    available = int(status.get("availableReplicas") or 0)
    updated = int(status.get("updatedReplicas") or 0)
    unavailable = int(status.get("unavailableReplicas") or 0)

    rollout_status = "healthy"
    if unavailable > 0 or ready < max(desired, 1):
        rollout_status = "degraded"
    elif updated < max(desired, 1):
        rollout_status = "progressing"

    messages = []
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        condition_type = condition.get("type") or "Unknown"
        condition_status = condition.get("status") or "Unknown"
        message = condition.get("message") or condition.get("reason") or ""
        messages.append(f"{condition_type}={condition_status} {message}".strip())

    return {
        "service": service_name,
        "namespace": namespace,
        "deployment": metadata.get("name") or service_name,
        "desired_replicas": desired,
        "ready_replicas": ready,
        "available_replicas": available,
        "updated_replicas": updated,
        "unavailable_replicas": unavailable,
        "rollout_status": rollout_status,
        "conditions": messages[:5],
    }


def _normalize_k8s_pods(payload: dict, service_name: str, namespace: str) -> list[dict]:
    if not isinstance(payload, dict):
        return []

    pods = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        container_statuses = status.get("containerStatuses") or []
        restart_count = sum(int(cs.get("restartCount") or 0) for cs in container_statuses if isinstance(cs, dict))
        ready = all(bool(cs.get("ready")) for cs in container_statuses) if container_statuses else False
        pods.append({
            "service": service_name,
            "namespace": namespace,
            "name": metadata.get("name"),
            "phase": status.get("phase") or "Unknown",
            "ready": ready,
            "restart_count": restart_count,
            "node_name": status.get("nodeName"),
            "started_at": status.get("startTime") or "",
        })
    return pods


def _normalize_k8s_events(payload: dict, service_name: str, namespace: str, pod_names: set[str]) -> list[dict]:
    if not isinstance(payload, dict):
        return []

    events = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        involved = item.get("involvedObject") or {}
        involved_name = involved.get("name") or ""
        if pod_names and involved_name not in pod_names and service_name not in involved_name:
            continue
        events.append({
            "service": service_name,
            "namespace": namespace,
            "type": item.get("type") or "Normal",
            "reason": item.get("reason") or "Unknown",
            "message": item.get("message") or "",
            "object_kind": involved.get("kind") or "",
            "object_name": involved_name,
            "timestamp": item.get("lastTimestamp") or item.get("eventTime") or (item.get("metadata") or {}).get("creationTimestamp") or "",
            "count": int(item.get("count") or 1),
        })
    return events


def _k8s_observability_from_api(service_name: str, namespace: str | None = None) -> dict | None:
    ns = (namespace or _k8s_default_namespace()).strip() or _k8s_default_namespace()
    payload = _request_json(f"/k8s/observability/{service_name}", query={"namespace": ns})
    if not isinstance(payload, dict):
        return None

    rollout = payload.get("rollout") if isinstance(payload.get("rollout"), dict) else None
    pods = [item for item in (payload.get("pods") or []) if isinstance(item, dict)]
    events = [item for item in (payload.get("events") or []) if isinstance(item, dict)]
    if not rollout and not pods and not events:
        return None

    restarting_pods = [pod for pod in pods if int(pod.get("restart_count") or 0) > 0]
    unhealthy_pods = [pod for pod in pods if pod.get("phase") != "Running" or not pod.get("ready")]
    return {
        "service": service_name,
        "namespace": ns,
        "rollout": rollout,
        "pods": pods[:10],
        "events": events[:10],
        "summary": {
            "pod_count": len(pods),
            "restarting_pods": len(restarting_pods),
            "unhealthy_pods": len(unhealthy_pods),
            "event_count": len(events),
        },
    }


def _k8s_observability_from_cluster(service_name: str, namespace: str | None = None) -> dict | None:
    if not _k8s_base_url():
        return None

    ns = (namespace or _k8s_default_namespace()).strip() or _k8s_default_namespace()
    service_label = _k8s_service_label()
    safe_service = service_name.replace("\\", "\\\\").replace('"', '\\"')

    rollout_payload = _request_k8s_json(f"/apis/apps/v1/namespaces/{parse.quote(ns, safe='')}/deployments/{parse.quote(service_name, safe='')}")
    pods_payload = _request_k8s_json(
        f"/api/v1/namespaces/{parse.quote(ns, safe='')}/pods",
        query={"labelSelector": f"{service_label}={safe_service}"},
    )
    events_payload = _request_k8s_json(
        f"/api/v1/namespaces/{parse.quote(ns, safe='')}/events",
        query={"fieldSelector": f"involvedObject.namespace={ns}"},
    )

    rollout = _normalize_k8s_rollout(rollout_payload, service_name, ns) if rollout_payload else None
    pods = _normalize_k8s_pods(pods_payload, service_name, ns) if pods_payload else []
    pod_names = {pod["name"] for pod in pods if pod.get("name")}
    events = _normalize_k8s_events(events_payload, service_name, ns, pod_names) if events_payload else []

    if not rollout and not pods and not events:
        return None

    restarting_pods = [pod for pod in pods if int(pod.get("restart_count") or 0) > 0]
    unhealthy_pods = [pod for pod in pods if pod.get("phase") != "Running" or not pod.get("ready")]
    return {
        "service": service_name,
        "namespace": ns,
        "rollout": rollout,
        "pods": pods[:10],
        "events": events[:10],
        "summary": {
            "pod_count": len(pods),
            "restarting_pods": len(restarting_pods),
            "unhealthy_pods": len(unhealthy_pods),
            "event_count": len(events),
        },
    }


def get_external_k8s_observability(service_name: str, namespace: str | None = None) -> dict | None:
    payload = _k8s_observability_from_api(service_name, namespace=namespace)
    if payload:
        return payload
    return _k8s_observability_from_cluster(service_name, namespace=namespace)


def get_external_services():
    payload = _request_json("/services")
    if isinstance(payload, list):
        normalized = [_normalize_service(item) for item in payload]
        return [item for item in normalized if item]
    if isinstance(payload, dict):
        services = payload.get("services")
        if isinstance(services, list):
            normalized = [_normalize_service(item) for item in services]
            return [item for item in normalized if item]

    prom_services = _get_prom_services()
    if prom_services:
        return prom_services

    targets = get_target_services()
    return targets or None


def get_external_service_status(service_name: str):
    payload = _request_json(f"/services/{service_name}")
    normalized = _normalize_service(payload) if isinstance(payload, dict) else None
    if normalized:
        return normalized
    if isinstance(payload, dict):
        service = payload.get("service")
        if isinstance(service, dict):
            normalized = _normalize_service(service)
            if normalized:
                return normalized

    prom_status = _get_prom_service_status(service_name)
    if prom_status:
        return prom_status

    return get_target_service_status(service_name)


def get_external_metrics(service_name: str):
    payload = _request_json(f"/metrics/{service_name}")
    normalized = _normalize_metrics(payload) if isinstance(payload, dict) else None
    if normalized:
        return normalized
    if isinstance(payload, dict):
        metrics = payload.get("metrics")
        if isinstance(metrics, dict):
            normalized = _normalize_metrics(metrics)
            if normalized:
                return normalized

    prom_metrics = _get_prom_metrics(service_name)
    if prom_metrics:
        return prom_metrics

    return get_target_metrics(service_name)


def get_external_logs(service_name: str, limit: int = 10):
    payload = _request_json("/logs", query={"service_name": service_name, "limit": limit})
    normalized = _normalize_logs(payload)
    if normalized is not None:
        return normalized

    loki_logs = _loki_query(service_name, limit=limit)
    if loki_logs is not None:
        return loki_logs

    return get_target_logs(service_name=service_name, limit=limit)


def get_external_alerts(service_name: str | None = None, unresolved_only: bool = True, limit: int = 10):
    payload = _request_json(
        "/alerts",
        query={
            "service_name": service_name,
            "unresolved_only": str(unresolved_only).lower(),
            "limit": limit,
        },
    )
    normalized = _normalize_alerts(payload)
    if normalized is not None:
        return normalized

    prom_alerts = _prom_alerts(service_name=service_name, limit=limit)
    if prom_alerts is not None:
        return prom_alerts

    return get_target_alerts(service_name=service_name, unresolved_only=unresolved_only, limit=limit)
