import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.generate_trial_env import (
        generate_trial_environment,
        read_trial_environment,
        validate_upgrade_contact_url,
        write_trial_environment,
    )
except ModuleNotFoundError:
    from generate_trial_env import (
        generate_trial_environment,
        read_trial_environment,
        validate_upgrade_contact_url,
        write_trial_environment,
    )


DELIVERY_SCHEMA_VERSION = 1
SECRET_KEYS = (
    "SRE_POSTGRES_PASSWORD",
    "SRE_ADMIN_API_KEY",
    "SRE_TRIAL_ACTIVATION_TOKEN",
    "EXECUTION_GUARD_TOKEN",
)
PUBLIC_ENV_KEYS = (
    "SRE_UPGRADE_CONTACT_URL",
    "SRE_TRIAL_DAYS",
    "SRE_HTTP_PORT",
    "SRE_WORKSPACE_ID",
    "SRE_WORKSPACE_NAME",
)
_WORKSPACE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


def validate_workspace_id(value: str) -> str:
    normalized = value.strip().lower()
    if not _WORKSPACE_ID_RE.fullmatch(normalized):
        raise ValueError(
            "workspace id must be 1-40 lowercase letters, numbers, or internal hyphens"
        )
    return normalized


def validate_workspace_name(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 100:
        raise ValueError("workspace name must contain 1-100 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("workspace name must not contain control characters")
    if "$" in normalized:
        raise ValueError("workspace name must not contain dotenv interpolation")
    return normalized


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _write_private_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _delivery_guide(manifest: dict, output: Path) -> str:
    project_name = manifest["compose_project_name"]
    env_path = (output / ".env.trial.local").as_posix()
    return f"""# Trial delivery: {manifest['workspace_name']}

This directory was generated for one isolated, unclaimed trial instance.
Do not commit it, attach it to tickets, or reuse it for another customer.

## Verify before use

```powershell
python scripts/prepare_trial_delivery.py verify --delivery-dir \"{output.as_posix()}\"
```

## Start the isolated Compose project

```powershell
docker compose `
  --project-name {project_name} `
  --env-file \"{env_path}\" `
  -f compose.yaml `
  -f compose.trial.yaml `
  up --build --detach --wait --wait-timeout 180
```

The project name is part of the isolation boundary. Keep it unchanged so this
customer does not share containers, networks, or PostgreSQL volumes with a
different trial.

## Secret handoff

Only `SRE_TRIAL_ACTIVATION_TOKEN` is intended for the customer. Copy it from
`.env.trial.local` into a password manager and send it through a separate secure
channel. PostgreSQL, bootstrap admin, and execution-guard secrets remain with
the operator. Never paste the full environment file into chat, email, or a
support ticket.

## Render

Create a separate Blueprint for this customer. Set `SRE_WORKSPACE_ID` to
`{manifest['workspace_id']}` and `SRE_WORKSPACE_NAME` to
`{manifest['workspace_name']}`. Copy `SRE_ADMIN_API_KEY`,
`SRE_TRIAL_ACTIVATION_TOKEN`, and `EXECUTION_GUARD_TOKEN` into their matching
Render Secret fields. Render manages PostgreSQL through `DATABASE_URL`, so do
not copy `SRE_POSTGRES_PASSWORD` there. Service/database names and the
Blueprint itself must also be unique per customer.

## Stop without deleting customer data

Use the same `--project-name`, `--env-file`, and Compose files with `down`.
Do not add `--volumes` unless this trial has been formally approved for
permanent destruction and any required export/retention work is complete.
"""


def prepare_trial_delivery(
    output_dir: Path,
    *,
    workspace_id: str,
    workspace_name: str,
    upgrade_contact_url: str,
    trial_days: int = 14,
    http_port: int = 8000,
    now: datetime | None = None,
) -> dict:
    normalized_id = validate_workspace_id(workspace_id)
    normalized_name = validate_workspace_name(workspace_name)
    normalized_upgrade_url = validate_upgrade_contact_url(upgrade_contact_url)
    output = output_dir.expanduser().resolve()
    if output.exists():
        raise FileExistsError(
            "delivery directory already exists; create a new directory for rotation"
        )

    values = generate_trial_environment(
        upgrade_contact_url=normalized_upgrade_url,
        trial_days=trial_days,
        http_port=http_port,
        workspace_id=normalized_id,
        workspace_name=normalized_name,
    )
    secret_fingerprints = {key: _fingerprint(values[key]) for key in SECRET_KEYS}
    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    manifest = {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "delivery_id": str(uuid.uuid4()),
        "created_at": created_at.isoformat(),
        "intended_lifecycle": "unclaimed_trial",
        "workspace_id": normalized_id,
        "workspace_name": normalized_name,
        "compose_project_name": f"sre-trial-{normalized_id}",
        "trial_days": trial_days,
        "http_port": http_port,
        "upgrade_contact_url": normalized_upgrade_url,
        "customer_secret_key": "SRE_TRIAL_ACTIVATION_TOKEN",
        "operator_only_secret_keys": [
            "SRE_POSTGRES_PASSWORD",
            "SRE_ADMIN_API_KEY",
            "EXECUTION_GUARD_TOKEN",
        ],
        "secret_fingerprints": secret_fingerprints,
    }

    try:
        output.mkdir(parents=True, mode=0o700)
        try:
            os.chmod(output, 0o700)
        except OSError:
            pass
        write_trial_environment(output / ".env.trial.local", values)
        _write_private_text(
            output / "delivery.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        _write_private_text(output / "README.md", _delivery_guide(manifest, output))
        verify_trial_delivery(output)
    except Exception:
        if output.exists():
            shutil.rmtree(output)
        raise
    return manifest


def verify_trial_delivery(delivery_dir: Path) -> dict:
    delivery = delivery_dir.expanduser().resolve()
    env_path = delivery / ".env.trial.local"
    manifest_path = delivery / "delivery.json"
    guide_path = delivery / "README.md"
    if not delivery.is_dir() or not env_path.is_file() or not manifest_path.is_file():
        raise ValueError("delivery directory is incomplete")
    if not guide_path.is_file():
        raise ValueError("delivery guide is missing")

    values = read_trial_environment(env_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("delivery manifest must be an object")
    if manifest.get("schema_version") != DELIVERY_SCHEMA_VERSION:
        raise ValueError("unsupported delivery manifest schema")
    try:
        delivery_id = uuid.UUID(str(manifest.get("delivery_id") or ""))
        created_at = datetime.fromisoformat(str(manifest.get("created_at") or ""))
    except ValueError as exc:
        raise ValueError("invalid delivery identity metadata") from exc
    if delivery_id.version != 4 or created_at.tzinfo is None:
        raise ValueError("invalid delivery identity metadata")
    if manifest.get("intended_lifecycle") != "unclaimed_trial":
        raise ValueError("invalid delivery lifecycle")
    if manifest.get("customer_secret_key") != "SRE_TRIAL_ACTIVATION_TOKEN":
        raise ValueError("invalid customer secret ownership metadata")
    if set(manifest.get("operator_only_secret_keys") or []) != (
        set(SECRET_KEYS) - {"SRE_TRIAL_ACTIVATION_TOKEN"}
    ):
        raise ValueError("invalid operator secret ownership metadata")

    required_keys = set(SECRET_KEYS) | set(PUBLIC_ENV_KEYS)
    missing_keys = sorted(required_keys - set(values))
    if missing_keys:
        raise ValueError(f"delivery environment is missing keys: {', '.join(missing_keys)}")
    unexpected_keys = sorted(set(values) - required_keys)
    if unexpected_keys:
        raise ValueError(
            f"delivery environment contains unexpected keys: {', '.join(unexpected_keys)}"
        )

    expected_public = {
        "SRE_UPGRADE_CONTACT_URL": manifest.get("upgrade_contact_url"),
        "SRE_TRIAL_DAYS": str(manifest.get("trial_days")),
        "SRE_HTTP_PORT": str(manifest.get("http_port")),
        "SRE_WORKSPACE_ID": manifest.get("workspace_id"),
        "SRE_WORKSPACE_NAME": manifest.get("workspace_name"),
    }
    mismatched_public = sorted(
        key for key, expected in expected_public.items() if values.get(key) != expected
    )
    if mismatched_public:
        raise ValueError(
            f"delivery environment does not match manifest: {', '.join(mismatched_public)}"
        )

    secret_values = [values[key] for key in SECRET_KEYS]
    if any(len(value) < 32 for value in secret_values):
        raise ValueError("delivery contains a weak secret")
    if len(set(secret_values)) != len(secret_values):
        raise ValueError("delivery secrets must be unique")
    expected_fingerprints = manifest.get("secret_fingerprints") or {}
    mismatched_secrets = sorted(
        key
        for key in SECRET_KEYS
        if expected_fingerprints.get(key) != _fingerprint(values[key])
    )
    if mismatched_secrets:
        raise ValueError(
            f"delivery secret fingerprint mismatch: {', '.join(mismatched_secrets)}"
        )

    nonsecret_content = manifest_path.read_text(encoding="utf-8") + guide_path.read_text(
        encoding="utf-8"
    )
    if any(value in nonsecret_content for value in secret_values):
        raise ValueError("delivery secret leaked into a nonsecret artifact")

    workspace_id = str(manifest.get("workspace_id") or "")
    workspace_name = str(manifest.get("workspace_name") or "")
    upgrade_contact_url = str(manifest.get("upgrade_contact_url") or "")
    if validate_workspace_id(workspace_id) != workspace_id:
        raise ValueError("workspace id is not normalized")
    if validate_workspace_name(workspace_name) != workspace_name:
        raise ValueError("workspace name is not normalized")
    if validate_upgrade_contact_url(upgrade_contact_url) != upgrade_contact_url:
        raise ValueError("upgrade contact URL is not normalized")
    trial_days = manifest.get("trial_days")
    http_port = manifest.get("http_port")
    if not isinstance(trial_days, int) or not 1 <= trial_days <= 3650:
        raise ValueError("invalid delivery trial duration")
    if not isinstance(http_port, int) or not 1 <= http_port <= 65535:
        raise ValueError("invalid delivery HTTP port")
    expected_project = f"sre-trial-{manifest['workspace_id']}"
    if manifest.get("compose_project_name") != expected_project:
        raise ValueError("invalid Compose project isolation name")

    return {
        "valid": True,
        "delivery_id": manifest["delivery_id"],
        "workspace_id": manifest["workspace_id"],
        "compose_project_name": expected_project,
        "secret_count": len(SECRET_KEYS),
        "secrets_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare and verify one isolated customer trial delivery"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--workspace-id", required=True)
    create_parser.add_argument("--workspace-name", required=True)
    create_parser.add_argument("--upgrade-contact-url", required=True)
    create_parser.add_argument("--trial-days", type=int, default=14)
    create_parser.add_argument("--http-port", type=int, default=8000)
    create_parser.add_argument("--output-dir")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--delivery-dir", required=True)
    args = parser.parse_args()

    if args.action == "create":
        workspace_id = validate_workspace_id(args.workspace_id)
        output = Path(args.output_dir or f".trial-deliveries/{workspace_id}")
        prepare_trial_delivery(
            output,
            workspace_id=workspace_id,
            workspace_name=args.workspace_name,
            upgrade_contact_url=args.upgrade_contact_url,
            trial_days=args.trial_days,
            http_port=args.http_port,
        )
        report = verify_trial_delivery(output)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        print(f"Delivery prepared at: {output.expanduser().resolve()}")
        print("Secrets were written only to the private environment file.")
        return

    report = verify_trial_delivery(Path(args.delivery_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
