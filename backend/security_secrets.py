import os

from dotenv import load_dotenv

from backend.storage.repositories import get_app_setting, set_app_setting

load_dotenv()


SECRET_SETTING_KEYS = {
    "SRE_DATA_API_TOKEN",
    "PROMETHEUS_TOKEN",
    "LOKI_TOKEN",
    "K8S_API_TOKEN",
}


def insecure_database_secrets_enabled() -> bool:
    return os.getenv("SRE_ALLOW_INSECURE_DB_SECRETS", "false").strip().lower() == "true"


def secret_storage_mode() -> str:
    return "insecure_database_opt_in" if insecure_database_secrets_enabled() else "environment_only"


def get_runtime_secret(key: str) -> str | None:
    if key not in SECRET_SETTING_KEYS:
        raise ValueError(f"unsupported secret setting: {key}")
    environment_value = os.getenv(key)
    if environment_value:
        return environment_value
    if insecure_database_secrets_enabled():
        return get_app_setting(key)
    return None


def persist_runtime_secret(key: str, value: str | None) -> None:
    if key not in SECRET_SETTING_KEYS:
        raise ValueError(f"unsupported secret setting: {key}")
    if not insecure_database_secrets_enabled():
        raise PermissionError(
            "database secret storage is disabled; configure this secret through environment variables or an external secret manager"
        )
    set_app_setting(key, value)
