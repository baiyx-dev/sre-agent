import json
import os
from urllib import error, request

from backend.security_network import UnsafeOutboundUrl, validate_outbound_url
from backend.tools.deploy_tool import deploy_service
from backend.tools.rollback_tool import rollback_service


class ChangeExecutorConfigurationError(RuntimeError):
    pass


class SimulationChangeExecutor:
    name = "simulation"

    def execute(self, change: dict) -> dict:
        action_type = change["action_type"]
        service_name = change["service_name"]
        if action_type == "deploy":
            result = deploy_service(service_name, change["target_version"])
        else:
            result = rollback_service(service_name)
        return {
            **result,
            "executor": self.name,
            "verified": bool(result.get("success")),
        }


class WebhookChangeExecutor:
    name = "webhook"

    def __init__(self, url: str, token: str | None, timeout_seconds: int):
        try:
            self.url = validate_outbound_url(url)
        except UnsafeOutboundUrl as exc:
            raise ChangeExecutorConfigurationError(f"unsafe executor webhook URL: {exc}") from exc
        self.token = token
        self.timeout_seconds = timeout_seconds

    def execute(self, change: dict) -> dict:
        change_request_id = change.get("change_request_id") or "unknown"
        payload = {
            "change_request_id": change_request_id,
            "action_type": change["action_type"],
            "service_name": change["service_name"],
            "target_version": change.get("target_version"),
            "requested_by": change.get("requested_by"),
            "approved_by": change.get("approved_by"),
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": change_request_id,
            "X-SRE-Change-Request-Id": change_request_id,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        outbound_request = request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(outbound_request, timeout=self.timeout_seconds) as response:
                raw_body = response.read(1_048_577)
                if len(raw_body) > 1_048_576:
                    return self._failure("executor_response_too_large")
                response_payload = json.loads(raw_body.decode("utf-8"))
        except error.HTTPError as exc:
            return self._failure(f"executor_http_error:{exc.code}")
        except (error.URLError, TimeoutError):
            return self._failure("executor_connection_error")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._failure("executor_invalid_json")

        if not isinstance(response_payload, dict):
            return self._failure("executor_invalid_response_shape")
        verified = response_payload.get("verified") is True
        succeeded = response_payload.get("success") is True and verified
        if not succeeded:
            reason = response_payload.get("message") or (
                "executor_did_not_verify_change" if not verified else "executor_reported_failure"
            )
            return {
                **response_payload,
                "success": False,
                "verified": verified,
                "executor": self.name,
                "message": str(reason),
            }
        return {
            **response_payload,
            "success": True,
            "verified": True,
            "executor": self.name,
        }

    def _failure(self, message: str) -> dict:
        return {
            "success": False,
            "verified": False,
            "executor": self.name,
            "message": message,
        }


def get_change_executor():
    mode = os.getenv("SRE_CHANGE_EXECUTOR", "simulation").strip().lower()
    if mode == "simulation":
        return SimulationChangeExecutor()
    if mode == "webhook":
        url = os.getenv("SRE_CHANGE_EXECUTOR_WEBHOOK_URL", "").strip()
        if not url:
            raise ChangeExecutorConfigurationError("SRE_CHANGE_EXECUTOR_WEBHOOK_URL is required")
        raw_timeout = os.getenv("SRE_CHANGE_EXECUTOR_TIMEOUT_SECONDS", "30").strip()
        try:
            timeout_seconds = max(1, min(int(raw_timeout), 120))
        except ValueError as exc:
            raise ChangeExecutorConfigurationError(
                "SRE_CHANGE_EXECUTOR_TIMEOUT_SECONDS must be an integer"
            ) from exc
        return WebhookChangeExecutor(
            url=url,
            token=os.getenv("SRE_CHANGE_EXECUTOR_TOKEN", "").strip() or None,
            timeout_seconds=timeout_seconds,
        )
    raise ChangeExecutorConfigurationError(f"unsupported change executor: {mode}")


def change_executor_configuration_status() -> dict:
    mode = os.getenv("SRE_CHANGE_EXECUTOR", "simulation").strip().lower()
    url = os.getenv("SRE_CHANGE_EXECUTOR_WEBHOOK_URL", "").strip()
    url_safe = False
    if mode == "webhook" and url:
        try:
            validate_outbound_url(url)
            url_safe = True
        except UnsafeOutboundUrl:
            url_safe = False
    configured = mode == "simulation" or (mode == "webhook" and url_safe)
    return {
        "mode": mode,
        "configured": configured,
        "url_safe": url_safe if mode == "webhook" else None,
        "token_configured": bool(os.getenv("SRE_CHANGE_EXECUTOR_TOKEN", "").strip()),
    }
