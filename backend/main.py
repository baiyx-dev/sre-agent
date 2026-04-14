import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes_internal import router as internal_router
from backend.storage.db import init_db
from backend.storage.seed import seed_data
from backend.api.routes_services import router as services_router
from backend.api.routes_incidents import router as incidents_router
from backend.api.routes_chat import router as chat_router
from backend.api.routes_settings import router as settings_router
from backend.services.observability_service import (
    record_request,
    request_timer_elapsed_ms,
    request_timer_start,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sre-agent")

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
    request.state.request_id = request_id
    started_at = request_timer_start()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = request_timer_elapsed_ms(started_at)
        record_request(500, duration_ms)
        logger.exception(
            "unhandled_request_exception",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": round(duration_ms, 2),
            },
        )
        raise

    duration_ms = request_timer_elapsed_ms(started_at)
    record_request(response.status_code, duration_ms)
    response.headers["X-Request-Id"] = request_id
    logger.info(
        "request_completed request_id=%s method=%s path=%s status_code=%s duration_ms=%.2f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    logger.warning(
        "http_exception request_id=%s method=%s path=%s status_code=%s detail=%s",
        request_id,
        request.method,
        request.url.path,
        exc.status_code,
        exc.detail,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "request_failed",
            "detail": exc.detail,
            "request_id": request_id,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    logger.exception(
        "unhandled_exception request_id=%s method=%s path=%s",
        request_id,
        request.method,
        request.url.path,
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


app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")

app.include_router(services_router)
app.include_router(incidents_router)
app.include_router(chat_router)
app.include_router(settings_router)
app.include_router(internal_router)
