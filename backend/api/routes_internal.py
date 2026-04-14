from fastapi import APIRouter

from backend.services.observability_service import build_metrics_snapshot

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/metrics")
def internal_metrics():
    return {
        "service": "sre-agent",
        "metrics": build_metrics_snapshot(),
    }
