import argparse
import json
import sys
from urllib import error, request


def normalize_headers(headers: dict) -> dict[str, str]:
    """Return response headers with case-insensitive lookup keys."""
    return {str(name).lower(): value for name, value in headers.items()}


def call(
    base_url: str,
    path: str,
    *,
    api_key: str | None = None,
    method: str = "GET",
    payload: dict | None = None,
) -> tuple[int, dict | str, dict]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-SRE-API-Key"] = api_key
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    outgoing = request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(outgoing, timeout=10) as response:
            raw = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
            body = json.loads(raw) if "json" in content_type else raw
            headers = normalize_headers(dict(response.headers.items()))
            return response.status, body, headers
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = raw
        return exc.code, body, normalize_headers(dict(exc.headers.items()))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description="SRE Agent process-level smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", required=True)
    args = parser.parse_args()

    checks = []
    live_status, live, live_headers = call(args.base_url, "/health/live")
    require(live_status == 200 and live.get("status") == "ok", "liveness failed")
    require(len(live_headers.get("x-trace-id", "")) == 32, "trace id header missing")
    require(live_headers.get("traceparent", "").startswith("00-"), "traceparent missing")
    checks.append("liveness_and_trace")

    ready_status, ready, _ = call(args.base_url, "/health/ready")
    require(ready_status == 200 and ready.get("status") == "ready", f"readiness failed: {ready}")
    require(all(ready.get("checks", {}).values()), f"readiness checks failed: {ready}")
    if ready.get("details", {}).get("change_execution_mode") == "queued":
        require(
            ready["details"].get("active_worker_count", 0) >= 1,
            f"queued mode has no active worker: {ready}",
        )
    checks.append("readiness")

    unauthorized, _, _ = call(args.base_url, "/services/")
    require(unauthorized == 401, "protected route did not reject missing API key")
    checks.append("authentication_rejection")

    identity_status, identity, _ = call(args.base_url, "/auth/me", api_key=args.api_key)
    require(identity_status == 200 and identity.get("role") == "admin", "admin identity failed")
    checks.append("admin_identity")

    subscription_status, subscription, _ = call(
        args.base_url,
        "/billing/subscription",
        api_key=args.api_key,
    )
    subscription_state = subscription.get("subscription", {})
    require(
        subscription_status == 200 and subscription_state.get("access_allowed") is True,
        f"subscription access failed: {subscription}",
    )
    checks.append("subscription_access")

    statement_status, statement_preview, _ = call(
        args.base_url,
        "/billing/statements/preview?month=2000-01",
        api_key=args.api_key,
    )
    require(
        statement_status == 200 and statement_preview.get("period_closed") is True,
        f"billing statement preview failed: {statement_preview}",
    )
    checks.append("billing_statement_preview")

    services_status, services, _ = call(args.base_url, "/services/", api_key=args.api_key)
    require(services_status == 200 and isinstance(services.get("services"), list), "services failed")
    checks.append("services")

    chat_status, chat, _ = call(
        args.base_url,
        "/chat",
        api_key=args.api_key,
        method="POST",
        payload={"message": "payment-service status", "session_id": "ci-smoke-session"},
    )
    require(chat_status == 200 and chat.get("intent") == "status_query", f"chat failed: {chat}")
    checks.append("chat")

    deploy_status, deploy, _ = call(
        args.base_url,
        "/deploy",
        api_key=args.api_key,
        method="POST",
        payload={
            "service_name": "payment-service",
            "new_version": "v-ci-smoke",
            "dry_run": True,
        },
    )
    require(deploy_status == 200 and deploy.get("mode") == "dry_run", f"dry-run failed: {deploy}")
    checks.append("safe_dry_run")

    metrics_status, metrics, _ = call(args.base_url, "/metrics", api_key=args.api_key)
    require(metrics_status == 200 and "sre_agent_request_total" in metrics, "metrics failed")
    checks.append("metrics")

    print(json.dumps({"ok": True, "base_url": args.base_url, "checks": checks}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
