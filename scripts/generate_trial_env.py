import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse


def validate_upgrade_contact_url(value: str) -> str:
    normalized = value.strip()
    if "$" in normalized or any(ord(character) < 32 for character in normalized):
        raise ValueError("upgrade contact URL contains unsafe dotenv characters")
    parsed = urlparse(normalized)
    if parsed.scheme == "mailto" and parsed.path and not parsed.netloc:
        return normalized
    if (
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
    ):
        return normalized
    raise ValueError("upgrade contact URL must use https:// or mailto:")


def generate_trial_environment(
    *,
    upgrade_contact_url: str,
    trial_days: int = 14,
    http_port: int = 8000,
    workspace_id: str | None = None,
    workspace_name: str | None = None,
) -> dict[str, str]:
    if not 1 <= trial_days <= 3650:
        raise ValueError("trial days must be between 1 and 3650")
    if not 1 <= http_port <= 65535:
        raise ValueError("HTTP port must be between 1 and 65535")
    values = {
        "SRE_POSTGRES_PASSWORD": secrets.token_urlsafe(36),
        "SRE_ADMIN_API_KEY": f"sre_bootstrap_{secrets.token_urlsafe(36)}",
        "SRE_TRIAL_ACTIVATION_TOKEN": f"sre_trial_{secrets.token_urlsafe(36)}",
        "EXECUTION_GUARD_TOKEN": f"sre_guard_{secrets.token_urlsafe(36)}",
        "SRE_UPGRADE_CONTACT_URL": validate_upgrade_contact_url(
            upgrade_contact_url
        ),
        "SRE_TRIAL_DAYS": str(trial_days),
        "SRE_HTTP_PORT": str(http_port),
    }
    if workspace_id is not None:
        values["SRE_WORKSPACE_ID"] = workspace_id
    if workspace_name is not None:
        values["SRE_WORKSPACE_NAME"] = workspace_name
    return values


def _format_env_value(value: str) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9_./:@%+,=-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def read_trial_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, raw_value = stripped.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError("invalid trial environment line")
        if raw_value.startswith('"'):
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid quoted trial environment value") from exc
            if not isinstance(value, str):
                raise ValueError("trial environment values must be strings")
        else:
            value = raw_value
        if name in values:
            raise ValueError(f"duplicate trial environment key: {name}")
        values[name] = value
    return values


def write_trial_environment(
    output_path: Path,
    values: dict[str, str],
    *,
    force: bool = False,
) -> Path:
    output = output_path.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if force else "x"
    content = [
        "# Generated local production trial secrets. Never commit this file.",
        "# Rotating these values can invalidate recovery or an unclaimed invite.",
    ]
    content.extend(
        f"{name}={_format_env_value(value)}" for name, value in values.items()
    )
    content.append("")
    with output.open(mode, encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(content))
    try:
        os.chmod(output, 0o600)
    except OSError:
        pass
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate non-committed secrets for the local production trial stack"
    )
    parser.add_argument(
        "--output",
        default=".env.trial.local",
        help="Secret env file to create (default: .env.trial.local)",
    )
    parser.add_argument("--upgrade-contact-url", required=True)
    parser.add_argument("--trial-days", type=int, default=14)
    parser.add_argument("--http-port", type=int, default=8000)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Explicitly replace an existing file and rotate all generated secrets",
    )
    args = parser.parse_args()

    values = generate_trial_environment(
        upgrade_contact_url=args.upgrade_contact_url,
        trial_days=args.trial_days,
        http_port=args.http_port,
    )
    output = write_trial_environment(Path(args.output), values, force=args.force)
    print(f"Created trial environment: {output}")
    print("Secrets were written only to that file and were not printed.")
    print(
        "Next: docker compose --env-file "
        f"{output} -f compose.yaml -f compose.trial.yaml up --build --detach --wait"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
