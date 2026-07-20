import os
import hmac

from dotenv import load_dotenv

load_dotenv()


def is_execution_guard_enabled() -> bool:
    # Fail closed by default. Explicitly disable only for isolated demo/test environments.
    value = os.getenv("EXECUTION_GUARD_ENABLED", "true").strip().lower()
    return value in ("1", "true", "yes", "on")


def validate_execution_guard_token(token: str | None) -> tuple[bool, str | None]:
    expected = os.getenv("EXECUTION_GUARD_TOKEN", "")
    if not expected:
        return False, "guard_token_not_configured"
    if token is None or not hmac.compare_digest(token, expected):
        return False, "invalid_guard_token"
    return True, None


def execution_guard_configuration_status() -> dict:
    return {
        "enabled": is_execution_guard_enabled(),
        "token_configured": bool(os.getenv("EXECUTION_GUARD_TOKEN", "").strip()),
    }
