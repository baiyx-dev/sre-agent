import logging
import re
import secrets
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes_internal import prometheus_metrics, router as internal_router
from backend.storage.db import init_db
from backend.storage.seed import seed_data
from backend.api.routes_services import router as services_router
from backend.api.routes_incidents import router as incidents_router
from backend.api.routes_chat import router as chat_router
from backend.api.routes_settings import router as settings_router
from backend.api.routes_auth import router as auth_router
from backend.api.routes_changes import router as changes_router
from backend.api.routes_audit import router as audit_router
from backend.api.routes_health import router as health_router
from backend.api.routes_commercial import router as commercial_router
from backend.api.routes_trial import router as trial_router
from backend.security_auth import require_viewer
from backend.services.observability_service import (
    record_request,
    record_usage_metering_failure,
    request_timer_elapsed_ms,
    request_timer_start,
)
from backend.services.commercial_service import record_usage_event
from backend.services.llm_usage_service import (
    begin_llm_usage_capture,
    finish_llm_usage_capture,
)
from backend.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("sre-agent")

_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)


def _server_trace_context(traceparent: str | None) -> tuple[str, str]:
    match = _TRACEPARENT_RE.fullmatch((traceparent or "").strip().lower())
    if (
        match
        and match.group("version") != "ff"
        and match.group("trace_id") != "0" * 32
        and match.group("parent_id") != "0" * 16
    ):
        trace_id = match.group("trace_id")
        flags = match.group("flags")
    else:
        trace_id = secrets.token_hex(16)
        flags = "01"
    return trace_id, f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"

app = FastAPI(title="SRE Agent Demo")

BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"


@app.on_event("startup")
def startup():
    init_db()
    seed_data()


@app.middleware("http")
async def request_observability_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    trace_id, response_traceparent = _server_trace_context(
        request.headers.get("traceparent")
    )
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    started_at = request_timer_start()
    llm_capture_token = begin_llm_usage_capture()

    try:
        response = await call_next(request)
    except Exception:
        finish_llm_usage_capture(llm_capture_token)
        duration_ms = request_timer_elapsed_ms(started_at)
        record_request(500, duration_ms)
        logger.exception(
            "unhandled_request_exception",
            extra={
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": round(duration_ms, 2),
            },
        )
        raise

    llm_usage = finish_llm_usage_capture(llm_capture_token)
    duration_ms = request_timer_elapsed_ms(started_at)
    record_request(response.status_code, duration_ms)
    principal = getattr(request.state, "principal", None)
    if getattr(principal, "auth_source", None) == "workspace_api_key":
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        try:
            record_usage_event(
                principal.workspace_id,
                metric="api_request",
                route=f"{request.method} {route_path}",
                status_code=response.status_code,
                request_id=request_id,
            )
            billable_metric = {
                ("POST", "/chat"): "chat_request",
                ("POST", "/chat/confirm"): "change_confirmation",
                ("POST", "/changes/{change_request_id}/confirm"): "change_confirmation",
                ("POST", "/changes/{change_request_id}/redrive"): "change_redrive",
                ("POST", "/incidents/correlate"): "incident_correlation",
            }.get((request.method, route_path))
            if billable_metric and response.status_code < 500:
                record_usage_event(
                    principal.workspace_id,
                    metric=billable_metric,
                    route=f"{request.method} {route_path}",
                    status_code=response.status_code,
                    request_id=f"{request_id}:{billable_metric}",
                )
            usage_metadata = {
                "providers": llm_usage["providers"],
                "models": llm_usage["models"],
            }
            for metric, quantity in (
                ("llm_call", llm_usage["call_count"]),
                ("llm_input_token", llm_usage["input_tokens"]),
                ("llm_output_token", llm_usage["output_tokens"]),
                ("llm_cost_usd_micro", llm_usage["cost_usd_micros"]),
            ):
                if quantity > 0:
                    record_usage_event(
                        principal.workspace_id,
                        metric=metric,
                        quantity=quantity,
                        route=f"{request.method} {route_path}",
                        status_code=response.status_code,
                        request_id=f"{request_id}:{metric}",
                        metadata=usage_metadata,
                    )
        except Exception:
            record_usage_metering_failure()
            logger.exception(
                "usage_metering_failed",
                extra={
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "workspace_id": principal.workspace_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                },
            )
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Trace-Id"] = trace_id
    response.headers["traceparent"] = response_traceparent
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    logger.info(
        "request_completed",
        extra={
            "request_id": request_id,
            "trace_id": trace_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(duration_ms, 2),
            "workspace_id": getattr(principal, "workspace_id", None),
        },
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    trace_id = getattr(request.state, "trace_id", None)
    logger.warning(
        "http_exception detail=%s",
        exc.detail,
        extra={
            "request_id": request_id,
            "trace_id": trace_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": exc.status_code,
        },
    )
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "error": "request_failed",
            "detail": exc.detail,
            "request_id": request_id,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    trace_id = getattr(request.state, "trace_id", None)
    logger.exception(
        "unhandled_exception",
        extra={
            "request_id": request_id,
            "trace_id": trace_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": 500,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": "unexpected server error",
            "request_id": request_id,
        },
    )


@app.get("/")
def root():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/metrics", response_class=PlainTextResponse)
def metrics_export(_principal=Depends(require_viewer)):
    return prometheus_metrics()


app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")

app.include_router(services_router)
app.include_router(incidents_router)
app.include_router(chat_router)
app.include_router(settings_router)
app.include_router(internal_router)
app.include_router(auth_router)
app.include_router(changes_router)
app.include_router(audit_router)
app.include_router(health_router)
app.include_router(commercial_router)
app.include_router(trial_router)
