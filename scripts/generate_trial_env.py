import argparse
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse


def validate_upgrade_contact_url(value: str) -> str:
    normalized = value.strip()
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
) -> dict[str, str]:
    if not 1 <= trial_days <= 3650:
        raise ValueError("trial days must be between 1 and 3650")
    if not 1 <= http_port <= 65535:
        raise ValueError("HTTP port must be between 1 and 65535")
    return {
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
    content.extend(f"{name}={value}" for name, value in values.items())
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
