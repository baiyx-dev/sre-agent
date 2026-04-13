from fastapi import APIRouter, HTTPException

from backend.tools.service_tool import (
    list_services,
    get_service_status,
)
from backend.tools.metrics_tool import get_service_metrics
from backend.tools.logs_tool import get_recent_logs

router = APIRouter(prefix="/services", tags=["services"])


@router.get("/")
def get_all_services():
    return {"services": list_services()}


@router.get("/{service_name}")
def get_one_service(service_name: str):
    service = get_service_status(service_name)
    if not service:
        raise HTTPException(status_code=404, detail="service not found")
    return service


@router.get("/{service_name}/metrics")
def get_one_service_metrics(service_name: str):
    metrics = get_service_metrics(service_name)
    if not metrics:
        raise HTTPException(status_code=404, detail="service not found")
    return metrics


@router.get("/{service_name}/logs")
def get_one_service_logs(service_name: str, limit: int = 10):
    service = get_service_status(service_name)
    if not service:
        raise HTTPException(status_code=404, detail="service not found")
    return {
        "service": service_name,
        "logs": get_recent_logs(service_name, limit=limit)
    }
