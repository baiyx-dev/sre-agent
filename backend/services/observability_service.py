from collections import deque
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter

from backend.storage.repositories import get_change_queue_metrics, worker_heartbeat_status
from backend.services.incident_service import get_incident_metrics
from backend.services.commercial_service import get_usage_summary


_STARTED_AT = datetime.now(timezone.utc)
_LOCK = Lock()
_REQUEST_COUNT = 0
_SUCCESS_COUNT = 0
_ERROR_COUNT = 0
_USAGE_METERING_FAILURES = 0
_LATENCY_MS = deque(maxlen=1000)


def record_request(status_code: int, duration_ms: float) -> None:
    global _REQUEST_COUNT, _SUCCESS_COUNT, _ERROR_COUNT
    with _LOCK:
        _REQUEST_COUNT += 1
        if status_code < 500:
            _SUCCESS_COUNT += 1
        else:
            _ERROR_COUNT += 1
        _LATENCY_MS.append(round(duration_ms, 2))


def record_usage_metering_failure() -> None:
    global _USAGE_METERING_FAILURES
    with _LOCK:
        _USAGE_METERING_FAILURES += 1


def build_metrics_snapshot() -> dict:
    with _LOCK:
        request_count = _REQUEST_COUNT
        success_count = _SUCCESS_COUNT
        error_count = _ERROR_COUNT
        usage_metering_failures = _USAGE_METERING_FAILURES
        latencies = list(_LATENCY_MS)

    avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
    sorted_latencies = sorted(latencies)
    p95_latency = _percentile(sorted_latencies, 95)
    queue_metrics = get_change_queue_metrics()
    worker_metrics = worker_heartbeat_status()
    incident_metrics = get_incident_metrics()
    usage = get_usage_summary()
    return {
        "uptime_seconds": int((datetime.now(timezone.utc) - _STARTED_AT).total_seconds()),
        "request_count": request_count,
        "success_count": success_count,
        "error_count": error_count,
        "usage_metering_failures": usage_metering_failures,
        "success_rate_pct": _rate(success_count, request_count),
        "error_rate_pct": _rate(error_count, request_count),
        "avg_response_time_ms": avg_latency,
        "p95_response_time_ms": p95_latency,
        "window_size": len(latencies),
        "change_queue": queue_metrics,
        "change_workers": worker_metrics,
        "incidents": incident_metrics,
        "commercial_usage": usage,
    }


def request_timer_start() -> float:
    return perf_counter()


def request_timer_elapsed_ms(started_at: float) -> float:
    return (perf_counter() - started_at) * 1000


def _rate(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def _percentile(sorted_values: list[float], percentile: int) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, int((percentile / 100) * len(sorted_values)) - 1))
    return round(sorted_values[index], 2)
