import os

from fastapi import APIRouter, Response

from backend.security_auth import auth_configuration_status
from backend.security_execution_guard import execution_guard_configuration_status
from backend.security_secrets import insecure_database_secrets_enabled
from backend.storage.db import database_backend_name, get_conn, get_schema_status
from backend.executors.change_executor import change_executor_configuration_status
from backend.services.commercial_service import (
    production_write_entitled,
    workspace_configuration_status,
)
from backend.logging_config import logging_configuration_status
from backend.services.change_request_service import production_writes_enabled
from backend.services.trial_service import (
    public_trial_status,
    trial_activation_configuration_status,
)
from backend.storage.repositories import worker_heartbeat_status
from backend.tools.external_data_source import data_source_configuration_status

router = APIRouter(prefix="/health", tags=["health"])


def _database_check() -> tuple[bool, str]:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return True, "ok"
    except Exception:
        return False, "unavailable"


@router.get("/live")
def liveness():
    return {"status": "ok"}


@router.get("/ready")
def readiness(response: Response):
    environment = os.getenv("SRE_ENVIRONMENT", "development").strip().lower()
    production = environment == "production"
    database_ok, database_status = _database_check()
    try:
        schema = get_schema_status()
    except Exception:
        schema = {"compatible": False, "applied_version": None, "current_version": None}
    auth = auth_configuration_status()
    guard = execution_guard_configuration_status()
    executor = change_executor_configuration_status()
    workspace = workspace_configuration_status()
    log_configuration = logging_configuration_status()
    production_write_enabled = production_writes_enabled()
    write_entitled = production_write_entitled()
    data_sources = data_source_configuration_status()
    try:
        trial_activation = trial_activation_configuration_status()
        trial_public = public_trial_status()
    except Exception:
        trial_activation = {"enabled": False, "configured": False}
        trial_public = {"claim_available": False, "claimed": False}
    require_real_data_source = os.getenv(
        "SRE_REQUIRE_REAL_DATA_SOURCE",
        "true" if production else "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    trial_data_source_onboarding_grace = bool(
        trial_activation["enabled"] and workspace.get("plan") == "trial"
    )
    database_backend = database_backend_name()
    allow_production_sqlite = os.getenv(
        "SRE_ALLOW_PRODUCTION_SQLITE",
        "false",
    ).strip().lower() == "true"
    change_execution_mode = os.getenv(
        "SRE_CHANGE_EXECUTION_MODE",
        "synchronous",
    ).strip().lower()
    try:
        worker_heartbeat = worker_heartbeat_status()
    except Exception:
        worker_heartbeat = {
            "healthy": False,
            "max_age_seconds": None,
            "active_count": 0,
            "stale_count": 0,
            "latest_worker": None,
        }

    checks = {
        "database": database_ok,
        "database_schema": schema["compatible"],
        "production_database": (not production)
        or database_backend == "postgresql"
        or allow_production_sqlite,
        "authentication": (not auth["enabled"] and not production)
        or (
            auth["enabled"]
            and (auth["configured"] or trial_public["claim_available"])
        ),
        "trial_activation": trial_activation["configured"] or trial_public["claimed"],
        "execution_guard": (not guard["enabled"] and not production)
        or (guard["enabled"] and guard["token_configured"]),
        "secret_storage": not (production and insecure_database_secrets_enabled()),
        "change_executor": executor["configured"],
        "production_executor": (not production)
        or (not production_write_enabled)
        or (
            executor["mode"] == "webhook"
            and executor["configured"]
            and executor["token_configured"]
        ),
        "production_write_entitlement": (not production)
        or (not production_write_enabled)
        or write_entitled,
        "change_execution_mode": change_execution_mode in {"synchronous", "queued"},
        "change_worker": change_execution_mode != "queued"
        or worker_heartbeat["healthy"],
        "workspace": workspace["configured"] and workspace["active"],
        "subscription_configuration": workspace["subscription"]["effective_status"]
        not in {"configuration_error", "unavailable"},
        "logging": log_configuration["valid"],
        "real_data_source": (not require_real_data_source)
        or trial_data_source_onboarding_grace
        or data_sources["has_real_data_source"],
    }
    ready = all(checks.values())
    if not ready:
        response.status_code = 503
    return {
        "status": "ready" if ready else "not_ready",
        "environment": environment,
        "checks": checks,
        "details": {
            "database": database_status,
            "database_backend": database_backend,
            "database_schema_version": schema["applied_version"],
            "application_schema_version": schema["current_version"],
            "production_sqlite_override": allow_production_sqlite,
            "authentication_enabled": auth["enabled"],
            "trial_self_service_enabled": trial_activation["enabled"],
            "trial_claim_available": trial_public["claim_available"],
            "trial_claimed": trial_public["claimed"],
            "execution_guard_enabled": guard["enabled"],
            "change_executor_mode": executor["mode"],
            "production_write_enabled": production_write_enabled,
            "production_write_entitled": write_entitled,
            "change_executor_token_configured": executor["token_configured"],
            "change_execution_mode": change_execution_mode,
            "worker_heartbeat_max_age_seconds": worker_heartbeat["max_age_seconds"],
            "active_worker_count": worker_heartbeat["active_count"],
            "stale_worker_count": worker_heartbeat["stale_count"],
            "latest_worker": worker_heartbeat["latest_worker"],
            "workspace_id": workspace["workspace_id"],
            "workspace_plan": workspace["plan"],
            "subscription_status": workspace["subscription"]["effective_status"],
            "subscription_access_allowed": workspace["subscription"]["access_allowed"],
            "subscription_access_ends_at": workspace["subscription"].get("access_ends_at"),
            "log_format": log_configuration["format"],
            "log_level": log_configuration["level"],
            "require_real_data_source": require_real_data_source,
            "trial_data_source_onboarding_grace": trial_data_source_onboarding_grace,
            "configured_data_sources": data_sources["configured_sources"],
            "unsafe_data_sources": data_sources["unsafe_sources"],
            "monitored_target_count": data_sources["monitored_target_count"],
        },
    }
