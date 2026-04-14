from collections import deque
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter


_STARTED_AT = datetime.now(timezone.utc)
_LOCK = Lock()
_REQUEST_COUNT = 0
_SUCCESS_COUNT = 0
_ERROR_COUNT = 0
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


def build_metrics_snapshot() -> dict:
    with _LOCK:
        request_count = _REQUEST_COUNT
        success_count = _SUCCESS_COUNT
        error_count = _ERROR_COUNT
        latencies = list(_LATENCY_MS)

    avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
    sorted_latencies = sorted(latencies)
    p95_latency = _percentile(sorted_latencies, 95)
    return {
        "uptime_seconds": int((datetime.now(timezone.utc) - _STARTED_AT).total_seconds()),
        "request_count": request_count,
        "success_count": success_count,
        "error_count": error_count,
        "success_rate_pct": _rate(success_count, request_count),
        "error_rate_pct": _rate(error_count, request_count),
        "avg_response_time_ms": avg_latency,
        "p95_response_time_ms": p95_latency,
        "window_size": len(latencies),
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
