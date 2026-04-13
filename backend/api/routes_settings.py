import json
from urllib import error, request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.storage.repositories import (
    delete_monitored_target,
    get_app_setting,
    list_monitored_targets,
    set_app_setting,
    upsert_monitored_target,
)

router = APIRouter(prefix="/settings", tags=["settings"])


class DataSourceConfigRequest(BaseModel):
    sre_data_api_base: str | None = None
    sre_data_api_token: str | None = None
    prometheus_base_url: str | None = None
    prometheus_token: str | None = None
    prometheus_service_label: str | None = None
    loki_base_url: str | None = None
    loki_token: str | None = None
    loki_service_label: str | None = None
    prom_query_up: str | None = None
    prom_query_replicas: str | None = None
    prom_query_error_rate: str | None = None
    prom_query_cpu: str | None = None
    prom_query_memory: str | None = None
    prom_query_latency_p95_ms: str | None = None
    prom_alert_query: str | None = None
    loki_query_template: str | None = None


class DataSourceTestRequest(BaseModel):
    sre_data_api_base: str | None = None
    sre_data_api_token: str | None = None
    prometheus_base_url: str | None = None
    prometheus_token: str | None = None
    prometheus_service_label: str | None = None
    loki_base_url: str | None = None
    loki_token: str | None = None
    loki_service_label: str | None = None
    prom_query_up: str | None = None
    prom_query_replicas: str | None = None
    prom_query_error_rate: str | None = None
    prom_query_cpu: str | None = None
    prom_query_memory: str | None = None
    prom_query_latency_p95_ms: str | None = None
    prom_alert_query: str | None = None
    loki_query_template: str | None = None


class MonitoredTargetRequest(BaseModel):
    name: str
    base_url: str


def _normalize_url(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"http://{raw}"


@router.get("/data-source")
def get_data_source_config():
    return {
        "sre_data_api_base": get_app_setting("SRE_DATA_API_BASE"),
        "sre_data_api_token": get_app_setting("SRE_DATA_API_TOKEN"),
        "prometheus_base_url": get_app_setting("PROMETHEUS_BASE_URL"),
        "prometheus_token": get_app_setting("PROMETHEUS_TOKEN"),
        "prometheus_service_label": get_app_setting("PROMETHEUS_SERVICE_LABEL"),
        "loki_base_url": get_app_setting("LOKI_BASE_URL"),
        "loki_token": get_app_setting("LOKI_TOKEN"),
        "loki_service_label": get_app_setting("LOKI_SERVICE_LABEL"),
        "prom_query_up": get_app_setting("PROM_QUERY_UP"),
        "prom_query_replicas": get_app_setting("PROM_QUERY_REPLICAS"),
        "prom_query_error_rate": get_app_setting("PROM_QUERY_ERROR_RATE"),
        "prom_query_cpu": get_app_setting("PROM_QUERY_CPU"),
        "prom_query_memory": get_app_setting("PROM_QUERY_MEMORY"),
        "prom_query_latency_p95_ms": get_app_setting("PROM_QUERY_LATENCY_P95_MS"),
        "prom_alert_query": get_app_setting("PROM_ALERT_QUERY"),
        "loki_query_template": get_app_setting("LOKI_QUERY_TEMPLATE"),
    }


@router.put("/data-source")
def update_data_source_config(req: DataSourceConfigRequest):
    payload = req.dict(exclude_unset=True)

    if "sre_data_api_base" in payload:
        base_value = _normalize_url(payload.get("sre_data_api_base"))
        if base_value and not (base_value.startswith("http://") or base_value.startswith("https://")):
            raise HTTPException(status_code=400, detail="invalid SRE_DATA_API_BASE: must start with http:// or https://")
        set_app_setting("SRE_DATA_API_BASE", base_value)

    if "sre_data_api_token" in payload:
        token_value = (payload.get("sre_data_api_token") or "").strip() or None
        set_app_setting("SRE_DATA_API_TOKEN", token_value)

    if "prometheus_base_url" in payload:
        set_app_setting("PROMETHEUS_BASE_URL", _normalize_url(payload.get("prometheus_base_url")))

    if "prometheus_token" in payload:
        set_app_setting("PROMETHEUS_TOKEN", (payload.get("prometheus_token") or "").strip() or None)

    if "prometheus_service_label" in payload:
        set_app_setting("PROMETHEUS_SERVICE_LABEL", (payload.get("prometheus_service_label") or "").strip() or None)

    if "loki_base_url" in payload:
        set_app_setting("LOKI_BASE_URL", _normalize_url(payload.get("loki_base_url")))

    if "loki_token" in payload:
        set_app_setting("LOKI_TOKEN", (payload.get("loki_token") or "").strip() or None)

    if "loki_service_label" in payload:
        set_app_setting("LOKI_SERVICE_LABEL", (payload.get("loki_service_label") or "").strip() or None)

    for field, key in [
        ("prom_query_up", "PROM_QUERY_UP"),
        ("prom_query_replicas", "PROM_QUERY_REPLICAS"),
        ("prom_query_error_rate", "PROM_QUERY_ERROR_RATE"),
        ("prom_query_cpu", "PROM_QUERY_CPU"),
        ("prom_query_memory", "PROM_QUERY_MEMORY"),
        ("prom_query_latency_p95_ms", "PROM_QUERY_LATENCY_P95_MS"),
        ("prom_alert_query", "PROM_ALERT_QUERY"),
        ("loki_query_template", "LOKI_QUERY_TEMPLATE"),
    ]:
        if field in payload:
            set_app_setting(key, (payload.get(field) or "").strip() or None)

    return {
        "ok": True,
        "sre_data_api_base": get_app_setting("SRE_DATA_API_BASE"),
        "sre_data_api_token": get_app_setting("SRE_DATA_API_TOKEN"),
        "prometheus_base_url": get_app_setting("PROMETHEUS_BASE_URL"),
        "prometheus_token": get_app_setting("PROMETHEUS_TOKEN"),
        "prometheus_service_label": get_app_setting("PROMETHEUS_SERVICE_LABEL"),
        "loki_base_url": get_app_setting("LOKI_BASE_URL"),
        "loki_token": get_app_setting("LOKI_TOKEN"),
        "loki_service_label": get_app_setting("LOKI_SERVICE_LABEL"),
        "prom_query_up": get_app_setting("PROM_QUERY_UP"),
        "prom_query_replicas": get_app_setting("PROM_QUERY_REPLICAS"),
        "prom_query_error_rate": get_app_setting("PROM_QUERY_ERROR_RATE"),
        "prom_query_cpu": get_app_setting("PROM_QUERY_CPU"),
        "prom_query_memory": get_app_setting("PROM_QUERY_MEMORY"),
        "prom_query_latency_p95_ms": get_app_setting("PROM_QUERY_LATENCY_P95_MS"),
        "prom_alert_query": get_app_setting("PROM_ALERT_QUERY"),
        "loki_query_template": get_app_setting("LOKI_QUERY_TEMPLATE"),
    }


def _probe_services(base_url: str, token: str | None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = base_url.rstrip("/") + "/services"
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            payload = json.loads(body)
            services = []
            if isinstance(payload, list):
                services = payload
            elif isinstance(payload, dict):
                services = payload.get("services") if isinstance(payload.get("services"), list) else []
            return {
                "ok": True,
                "status_code": getattr(resp, "status", 200),
                "service_count": len(services),
                "sample_services": [
                    svc.get("name") or svc.get("service_name")
                    for svc in services[:5]
                    if isinstance(svc, dict)
                ],
                "error": None,
            }
    except error.HTTPError as e:
        return {
            "ok": False,
            "status_code": e.code,
            "service_count": 0,
            "sample_services": [],
            "error": f"http_error:{e.code}",
        }
    except error.URLError:
        return {
            "ok": False,
            "status_code": None,
            "service_count": 0,
            "sample_services": [],
            "error": "connection_error",
        }
    except TimeoutError:
        return {
            "ok": False,
            "status_code": None,
            "service_count": 0,
            "sample_services": [],
            "error": "timeout",
        }
    except json.JSONDecodeError:
        return {
            "ok": False,
            "status_code": 200,
            "service_count": 0,
            "sample_services": [],
            "error": "invalid_json",
        }


def _probe_prometheus(base_url: str | None, token: str | None, service_label: str | None):
    if not base_url:
        return {"configured": False, "ok": False, "error": "missing_base_url"}

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    label = (service_label or "service").strip() or "service"
    url = base_url.rstrip("/") + f"/api/v1/label/{label}/values"
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            values = payload.get("data") if isinstance(payload, dict) else []
            values = values if isinstance(values, list) else []
            return {
                "configured": True,
                "ok": True,
                "status_code": getattr(resp, "status", 200),
                "service_count": len(values),
                "sample_services": [str(item) for item in values[:5]],
                "error": None,
            }
    except error.HTTPError as e:
        return {"configured": True, "ok": False, "status_code": e.code, "service_count": 0, "sample_services": [], "error": f"http_error:{e.code}"}
    except error.URLError:
        return {"configured": True, "ok": False, "status_code": None, "service_count": 0, "sample_services": [], "error": "connection_error"}
    except TimeoutError:
        return {"configured": True, "ok": False, "status_code": None, "service_count": 0, "sample_services": [], "error": "timeout"}
    except json.JSONDecodeError:
        return {"configured": True, "ok": False, "status_code": 200, "service_count": 0, "sample_services": [], "error": "invalid_json"}


def _probe_loki(base_url: str | None, token: str | None):
    if not base_url:
        return {"configured": False, "ok": False, "error": "missing_base_url"}

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = base_url.rstrip("/") + "/loki/api/v1/labels"
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            data = payload.get("data") if isinstance(payload, dict) else []
            labels = data if isinstance(data, list) else []
            return {
                "configured": True,
                "ok": True,
                "status_code": getattr(resp, "status", 200),
                "label_count": len(labels),
                "sample_labels": [str(item) for item in labels[:5]],
                "error": None,
            }
    except error.HTTPError as e:
        return {"configured": True, "ok": False, "status_code": e.code, "label_count": 0, "sample_labels": [], "error": f"http_error:{e.code}"}
    except error.URLError:
        return {"configured": True, "ok": False, "status_code": None, "label_count": 0, "sample_labels": [], "error": "connection_error"}
    except TimeoutError:
        return {"configured": True, "ok": False, "status_code": None, "label_count": 0, "sample_labels": [], "error": "timeout"}
    except json.JSONDecodeError:
        return {"configured": True, "ok": False, "status_code": 200, "label_count": 0, "sample_labels": [], "error": "invalid_json"}


@router.post("/data-source/test")
def test_data_source(req: DataSourceTestRequest):
    base_value = _normalize_url(req.sre_data_api_base if req.sre_data_api_base is not None else get_app_setting("SRE_DATA_API_BASE")) or ""
    token_value = (req.sre_data_api_token if req.sre_data_api_token is not None else get_app_setting("SRE_DATA_API_TOKEN") or "").strip() or None
    prometheus_base = _normalize_url(req.prometheus_base_url if hasattr(req, "prometheus_base_url") and req.prometheus_base_url is not None else get_app_setting("PROMETHEUS_BASE_URL")) or ""
    prometheus_token = (getattr(req, "prometheus_token", None) if hasattr(req, "prometheus_token") and getattr(req, "prometheus_token") is not None else get_app_setting("PROMETHEUS_TOKEN") or "").strip() or None
    prometheus_label = (getattr(req, "prometheus_service_label", None) if hasattr(req, "prometheus_service_label") and getattr(req, "prometheus_service_label") is not None else get_app_setting("PROMETHEUS_SERVICE_LABEL") or "service").strip() or "service"
    loki_base = _normalize_url(getattr(req, "loki_base_url", None) if hasattr(req, "loki_base_url") and getattr(req, "loki_base_url") is not None else get_app_setting("LOKI_BASE_URL")) or ""
    loki_token = (getattr(req, "loki_token", None) if hasattr(req, "loki_token") and getattr(req, "loki_token") is not None else get_app_setting("LOKI_TOKEN") or "").strip() or None

    probes = {}
    if base_value:
        probes["sre_api"] = _probe_services(base_value, token_value)
    if prometheus_base:
        probes["prometheus"] = _probe_prometheus(prometheus_base, prometheus_token, prometheus_label)
    if loki_base:
        probes["loki"] = _probe_loki(loki_base, loki_token)

    if not probes:
        return {"ok": False, "error": "missing_data_source", "message": "未配置 SRE API、Prometheus 或 Loki 地址"}

    ok_sources = [name for name, probe in probes.items() if probe.get("ok")]
    return {
        "ok": len(ok_sources) > 0,
        "message": "数据源连接成功" if ok_sources else "数据源连接失败",
        "connected_sources": ok_sources,
        "probes": probes,
    }


@router.get("/targets")
def get_monitored_targets():
    return {"targets": list_monitored_targets()}


@router.post("/targets")
def create_or_update_target(req: MonitoredTargetRequest):
    name = req.name.strip()
    base_url = _normalize_url(req.base_url) or ""
    if not name:
        raise HTTPException(status_code=400, detail="target name is required")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise HTTPException(status_code=400, detail="target base_url must start with http:// or https://")
    saved = upsert_monitored_target(name=name, base_url=base_url)
    return {"ok": True, "target": saved}


@router.delete("/targets/{name}")
def delete_target(name: str):
    deleted = delete_monitored_target(name)
    if not deleted:
        raise HTTPException(status_code=404, detail="target not found")
    return {"ok": True, "deleted_target": name}
