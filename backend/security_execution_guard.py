import os

from dotenv import load_dotenv

load_dotenv()


def is_execution_guard_enabled() -> bool:
    value = os.getenv("EXECUTION_GUARD_ENABLED", "false").strip().lower()
    return value in ("1", "true", "yes", "on")


def validate_execution_guard_token(token: str | None) -> tuple[bool, str | None]:
    expected = os.getenv("EXECUTION_GUARD_TOKEN", "")
    if not expected:
        return False, "guard_token_not_configured"
    if token != expected:
        return False, "invalid_guard_token"
    return True, None
