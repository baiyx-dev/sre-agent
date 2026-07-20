import argparse
import json
import os
import sys
import uuid
from pathlib import Path

try:
    from scripts.generate_trial_env import read_trial_environment
    from scripts.smoke_test import call, require
except ModuleNotFoundError:
    from generate_trial_env import read_trial_environment
    from smoke_test import call, require


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Destructive end-to-end smoke test for an unclaimed trial instance"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    token_source = parser.add_mutually_exclusive_group()
    token_source.add_argument(
        "--activation-token-env",
        default="SRE_TRIAL_ACTIVATION_TOKEN",
        help="Environment variable containing the activation token",
    )
    token_source.add_argument(
        "--activation-token-file",
        help=(
            "Dotenv file containing SRE_TRIAL_ACTIVATION_TOKEN; "
            "read as data, not shell code"
        ),
    )
    parser.add_argument(
        "--confirm-disposable-instance",
        action="store_true",
        help="Required because this permanently claims the trial instance",
    )
    args = parser.parse_args()

    require(
        args.confirm_disposable_instance,
        "refusing to claim an instance without --confirm-disposable-instance",
    )
    if args.activation_token_file:
        activation_token = read_trial_environment(
            Path(args.activation_token_file)
        ).get("SRE_TRIAL_ACTIVATION_TOKEN", "").strip()
        activation_token_source = "SRE_TRIAL_ACTIVATION_TOKEN in activation token file"
    else:
        activation_token = os.getenv(args.activation_token_env, "").strip()
        activation_token_source = args.activation_token_env
    require(
        len(activation_token) >= 32,
        f"{activation_token_source} must contain at least 32 characters",
    )

    checks: list[str] = []
    live_status, live, _ = call(args.base_url, "/health/live")
    require(live_status == 200 and live.get("status") == "ok", "liveness failed")
    checks.append("liveness")

    ready_status, ready, _ = call(args.base_url, "/health/ready")
    require(
        ready_status == 200
        and ready.get("status") == "ready"
        and ready.get("details", {}).get("trial_claim_available") is True,
        "unclaimed trial instance is not ready for activation",
    )
    checks.append("unclaimed_readiness")

    status_code, public_status, _ = call(args.base_url, "/trial/status")
    require(
        status_code == 200
        and public_status.get("claim_available") is True
        and public_status.get("claimed") is False
        and public_status.get("status") == "pending_activation",
        "public trial status is not pending activation",
    )
    checks.append("pending_public_status")

    unauthorized_status, _, _ = call(args.base_url, "/services/")
    require(unauthorized_status == 401, "protected route accepted an anonymous request")
    checks.append("anonymous_rejection")

    run_id = uuid.uuid4().hex[:12]
    activation_payload = {
        "activation_token": activation_token,
        "workspace_name": f"Trial smoke {run_id}",
        "admin_name": "Trial Smoke Admin",
        "contact_email": f"trial-smoke-{run_id}@example.invalid",
    }
    activation_status, activation, _ = call(
        args.base_url,
        "/trial/activate",
        method="POST",
        payload=activation_payload,
    )
    require(activation_status == 201, "trial activation failed")
    api_key = activation.get("api_key", "")
    require(
        api_key.startswith("sre_live_")
        and activation.get("role") == "admin"
        and activation.get("subscription", {}).get("access_allowed") is True,
        "activation did not issue a usable admin key",
    )
    checks.append("atomic_activation_and_key_issue")

    replay_status, _, _ = call(
        args.base_url,
        "/trial/activate",
        method="POST",
        payload=activation_payload,
    )
    require(replay_status == 409, "trial activation was not one-time")
    checks.append("one_time_activation")

    claimed_status_code, claimed_status, _ = call(args.base_url, "/trial/status")
    require(
        claimed_status_code == 200
        and claimed_status.get("claim_available") is False
        and claimed_status.get("claimed") is True,
        "public trial status did not become claimed",
    )

    identity_status, identity, _ = call(args.base_url, "/auth/me", api_key=api_key)
    require(
        identity_status == 200
        and identity.get("role") == "admin"
        and identity.get("auth_source") == "workspace_api_key",
        "issued workspace key failed authentication",
    )
    checks.append("issued_key_authentication")

    onboarding_status, onboarding, _ = call(
        args.base_url,
        "/trial/onboarding",
        api_key=api_key,
    )
    require(
        onboarding_status == 200
        and onboarding.get("progress_percent") == 20
        and onboarding.get("first_value_at") is None
        and onboarding.get("first_value_evidence_sources") == 0,
        "initial onboarding milestones are not evidence-backed",
    )
    checks.append("initial_onboarding")

    feedback_key = f"trial-smoke-feedback-{run_id}"
    feedback_payload = {
        "idempotency_key": feedback_key,
        "rating": 5,
        "outcome": "not_evaluated",
        "purchase_intent": "maybe",
        "missing_feature": None,
        "notes": "Disposable activation smoke test.",
        "contact_consent": False,
    }
    feedback_status, feedback, _ = call(
        args.base_url,
        "/trial/feedback",
        api_key=api_key,
        method="POST",
        payload=feedback_payload,
    )
    require(
        feedback_status == 201 and feedback.get("created") is True,
        "trial feedback creation failed",
    )
    replay_feedback_status, replay_feedback, _ = call(
        args.base_url,
        "/trial/feedback",
        api_key=api_key,
        method="POST",
        payload=feedback_payload,
    )
    require(
        replay_feedback_status == 201 and replay_feedback.get("created") is False,
        "trial feedback replay was not idempotent",
    )
    changed_feedback = {**feedback_payload, "rating": 4}
    conflict_status, _, _ = call(
        args.base_url,
        "/trial/feedback",
        api_key=api_key,
        method="POST",
        payload=changed_feedback,
    )
    require(conflict_status == 409, "changed feedback replay did not conflict")
    checks.append("idempotent_feedback")

    metrics_status, metrics, _ = call(
        args.base_url,
        "/trial/conversion-metrics",
        api_key=api_key,
    )
    require(
        metrics_status == 200
        and metrics.get("activation", {}).get("contact_email")
        == activation_payload["contact_email"]
        and metrics.get("feedback_summary", {}).get("count") == 1
        and metrics.get("feedback_summary", {}).get("contact_consent") == 0,
        "conversion metrics do not match activation and feedback facts",
    )
    checks.append("conversion_metrics")

    final_ready_status, final_ready, _ = call(args.base_url, "/health/ready")
    require(
        final_ready_status == 200
        and final_ready.get("status") == "ready"
        and final_ready.get("details", {}).get("trial_claimed") is True,
        "claimed trial instance is not ready",
    )
    checks.append("claimed_readiness")

    print(
        json.dumps(
            {
                "ok": True,
                "base_url": args.base_url,
                "workspace_id": activation.get("workspace_id"),
                "checks": checks,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
