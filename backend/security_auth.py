import hmac
import hashlib
import os
from dataclasses import dataclass
from typing import Callable

from fastapi import Header, HTTPException, Request
from dotenv import load_dotenv

from backend.services.commercial_service import (
    authenticate_workspace_api_key,
    count_active_workspace_api_keys,
    workspace_request_limit_reached,
)
from backend.storage.db import configured_workspace_id


load_dotenv()


_ROLE_LEVELS = {"viewer": 10, "operator": 20, "admin": 30}


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    workspace_id: str
    auth_source: str


def is_auth_enabled() -> bool:
    return os.getenv("SRE_AUTH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _configured_keys() -> list[tuple[str, str]]:
    result = []
    for role, env_name in (
        ("viewer", "SRE_VIEWER_API_KEY"),
        ("operator", "SRE_OPERATOR_API_KEY"),
        ("admin", "SRE_ADMIN_API_KEY"),
    ):
        value = os.getenv(env_name, "").strip()
        if value:
            result.append((role, value))
    return result


def auth_configuration_status() -> dict:
    configured_roles = [role for role, _ in _configured_keys()]
    try:
        workspace_key_count = count_active_workspace_api_keys()
    except Exception:
        workspace_key_count = 0
    return {
        "enabled": is_auth_enabled(),
        "configured": bool(configured_roles or workspace_key_count),
        "configured_roles": configured_roles,
        "workspace_key_count": workspace_key_count,
    }


def _extract_key(x_sre_api_key: str | None, authorization: str | None) -> str | None:
    if x_sre_api_key:
        return x_sre_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def require_role(required_role: str) -> Callable:
    if required_role not in _ROLE_LEVELS:
        raise ValueError(f"unknown role: {required_role}")

    def dependency(
        request: Request,
        x_sre_api_key: str | None = Header(default=None, alias="X-SRE-API-Key"),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> Principal:
        if not is_auth_enabled():
            principal = Principal(
                subject="local-demo",
                role="admin",
                workspace_id=configured_workspace_id(),
                auth_source="local",
            )
            request.state.principal = principal
            return principal

        configured = _configured_keys()
        try:
            workspace_key_count = count_active_workspace_api_keys()
        except Exception:
            workspace_key_count = 0
        if not configured and not workspace_key_count:
            raise HTTPException(status_code=503, detail="authentication is enabled but no API keys are configured")

        provided = _extract_key(x_sre_api_key, authorization)
        if not provided:
            raise HTTPException(
                status_code=401,
                detail="API key is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        matched_roles = [role for role, expected in configured if hmac.compare_digest(provided, expected)]
        if matched_roles:
            role = max(matched_roles, key=lambda item: _ROLE_LEVELS[item])
            key_fingerprint = hashlib.sha256(provided.encode("utf-8")).hexdigest()[:12]
            principal = Principal(
                subject=f"api-key:{role}:{key_fingerprint}",
                role=role,
                workspace_id=configured_workspace_id(),
                auth_source="environment",
            )
        else:
            workspace_key = authenticate_workspace_api_key(provided)
            if not workspace_key:
                raise HTTPException(status_code=401, detail="invalid API key")
            role = workspace_key["role"]
            if request.url.path != "/auth/me" and not request.url.path.startswith(
                "/billing/"
            ) and workspace_request_limit_reached(
                workspace_key["workspace_id"]
            ):
                raise HTTPException(
                    status_code=429,
                    detail="monthly workspace request limit reached",
                    headers={"Retry-After": "3600"},
                )
            principal = Principal(
                subject=f"workspace-key:{workspace_key['id']}",
                role=role,
                workspace_id=workspace_key["workspace_id"],
                auth_source="workspace_api_key",
            )
        if _ROLE_LEVELS[role] < _ROLE_LEVELS[required_role]:
            raise HTTPException(status_code=403, detail=f"{required_role} role is required")
        request.state.principal = principal
        return principal

    return dependency


require_viewer = require_role("viewer")
require_operator = require_role("operator")
require_admin = require_role("admin")


def principal_subject(principal: object) -> str:
    """Return a trusted audit subject for injected principals and internal calls."""
    if isinstance(principal, Principal):
        return principal.subject
    return "internal-call"


def principal_workspace_id(principal: object) -> str:
    if isinstance(principal, Principal):
        return principal.workspace_id
    return configured_workspace_id()
