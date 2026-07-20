import os
import unittest
from unittest.mock import patch
from urllib import error
from urllib.parse import parse_qs, urlparse
import json
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api.routes_chat import chat, confirm_action
from backend.api.routes_changes import (
    ChangeCancelRequest,
    ChangeConfirmRequest,
    cancel_change,
    confirm_change,
)
from backend.api.routes_incidents import (
    IncidentUpdateRequest,
    benchmark_replay,
    benchmark_run,
    benchmark_scenarios,
    change_incident,
    deploy,
    incident_detail,
    incidents,
    postmortem,
    rollback,
    timeline,
)
from backend.api.routes_settings import DataSourceConfigRequest, get_data_source_config, update_data_source_config
from backend.schemas.chat import ChatRequest, ConfirmActionRequest
from backend.api.routes_incidents import DeployRequest, RollbackRequest
from backend.agents.intent_router import extract_entities
from backend.main import app
from backend.security_network import UnsafeOutboundUrl, validate_outbound_url
from backend.storage.db import init_db
from backend.storage.db import (
    CURRENT_SCHEMA_VERSION,
    _PostgresCursor,
    _postgres_sql,
    get_schema_status,
    get_conn,
    is_postgres_database,
)
from backend.storage.repositories import (
    get_change_job,
    get_change_request,
    get_chat_session_context,
    save_execution_audit,
    touch_worker_heartbeat,
    verify_audit_ledger,
    worker_heartbeat_status,
)
from backend.services.change_worker_service import process_next_change_job
from backend.services.incident_service import correlate_alerts
from backend.services.commercial_service import (
    PlanEntitlementError,
    get_plan_entitlements,
    get_subscription_status,
    get_workspace,
    issue_workspace_api_key,
    revoke_workspace_api_key,
)
from backend.services.llm_usage_service import (
    begin_llm_usage_capture,
    finish_llm_usage_capture,
)
from backend.llm.provider import _request_chat_completion
from backend.storage.seed import seed_data
from backend.tools.service_tool import get_service_status
from backend.maintenance import (
    backup_database,
    purge_retained_data,
    restore_database,
    retention_preview,
    verify_sqlite_database,
)
from backend.analyzers.service_health import EvidenceBudget, ServiceHealthAnalyzer
from backend.tools.external_data_source import (
    data_source_configuration_status,
    get_external_k8s_observability,
    get_external_logs,
    get_external_metrics,
    get_external_services,
)
from scripts.smoke_test import normalize_headers


class MockHttpResponse:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self, size=-1):
        return self.payload if size is None or size < 0 else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        seed_data()

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ.pop("DEEPSEEK_API_BASE", None)
        os.environ["SRE_AUTH_ENABLED"] = "false"
        os.environ["EXECUTION_GUARD_ENABLED"] = "false"
        os.environ.pop("EXECUTION_GUARD_TOKEN", None)
        os.environ.pop("SRE_ALLOW_INSECURE_DB_SECRETS", None)
        os.environ["SRE_ENVIRONMENT"] = "development"
        os.environ.pop("SRE_CHANGE_EXECUTOR", None)
        os.environ.pop("SRE_CHANGE_EXECUTOR_WEBHOOK_URL", None)
        os.environ.pop("SRE_CHANGE_EXECUTOR_TOKEN", None)
        os.environ.pop("SRE_CHANGE_EXECUTION_MODE", None)
        os.environ.pop("SRE_CHANGE_JOB_MAX_ATTEMPTS", None)
        os.environ.pop("SRE_PLAN_PRICE_USD_MONTHLY", None)
        os.environ.pop("SRE_INFRA_COST_USD_MONTHLY", None)
        os.environ.pop("SRE_CUSTOMER_HOURLY_COST_USD", None)
        os.environ.pop("SRE_SUPPORT_HOURLY_COST_USD", None)
        os.environ["SRE_OUTBOUND_HOST_ALLOWLIST"] = "prom.example.com,loki.example.com,k8s.example.com,executor.example.com"
        os.environ.pop("SRE_ALLOW_PRIVATE_NETWORK_TARGETS", None)
        self.external_source_patchers = [
            patch("backend.tools.service_tool.get_external_services", return_value=None),
            patch("backend.tools.service_tool.get_external_service_status", return_value=None),
            patch("backend.tools.metrics_tool.get_external_metrics", return_value=None),
            patch("backend.tools.logs_tool.get_external_logs", return_value=None),
            patch("backend.tools.alert_tool.get_external_alerts", return_value=None),
        ]
        for patcher in self.external_source_patchers:
            patcher.start()

    def tearDown(self):
        self._stop_external_source_patches()

    def _stop_external_source_patches(self):
        for patcher in getattr(self, "external_source_patchers", []):
            patcher.stop()
        self.external_source_patchers = []

    def test_chat_status_query_shape_and_fallback_meta(self):
        data = chat(ChatRequest(message="payment-service 状态")).model_dump()

        self.assertIn("intent", data)
        self.assertIn("steps", data)
        self.assertIn("final_answer", data)
        self.assertIn("generation_source", data)
        self.assertIn("llm_provider", data)
        self.assertIn("used_fallback", data)
        self.assertIn("fallback_reason", data)

        self.assertEqual(data["intent"], "status_query")
        self.assertEqual(data["generation_source"], "fallback_no_api_key")
        self.assertTrue(data["used_fallback"])
        self.assertEqual(data["fallback_reason"], "missing_api_key")

    def test_settings_api_never_returns_secret_values(self):
        secret = "regression-secret-token"
        os.environ["SRE_ALLOW_INSECURE_DB_SECRETS"] = "true"
        try:
            updated = update_data_source_config(DataSourceConfigRequest(sre_data_api_token=secret))
            loaded = get_data_source_config()

            self.assertNotIn("sre_data_api_token", updated)
            self.assertNotIn("sre_data_api_token", loaded)
            self.assertTrue(updated["sre_data_api_token_configured"])
            self.assertTrue(loaded["sre_data_api_token_configured"])
            self.assertNotIn(secret, json.dumps(updated))
            self.assertNotIn(secret, json.dumps(loaded))
        finally:
            update_data_source_config(DataSourceConfigRequest(sre_data_api_token=""))
            os.environ.pop("SRE_ALLOW_INSECURE_DB_SECRETS", None)

    def test_database_secret_storage_is_disabled_by_default(self):
        os.environ.pop("SRE_ALLOW_INSECURE_DB_SECRETS", None)
        with self.assertRaises(HTTPException) as rejected:
            update_data_source_config(
                DataSourceConfigRequest(prometheus_token="must-not-be-stored")
            )
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(get_data_source_config()["secret_storage_mode"], "environment_only")

    @unittest.skipIf(is_postgres_database(), "SQLite backup API is not used for PostgreSQL")
    def test_database_backup_verify_and_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup = backup_database(root / "backups")
            backup_path = Path(backup["path"])
            manifest_path = Path(backup["manifest_path"])

            self.assertTrue(backup_path.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(backup["integrity"], "ok")
            self.assertGreater(backup["table_count"], 0)

            verified = verify_sqlite_database(backup_path)
            self.assertEqual(verified["sha256"], backup["sha256"])

            restored = restore_database(
                backup_path,
                root / "restored" / "sre_agent.db",
            )
            self.assertEqual(restored["integrity"], "ok")
            self.assertEqual(restored["table_count"], backup["table_count"])

    @unittest.skipIf(is_postgres_database(), "isolated SQLite path is used for retention test")
    def test_retention_is_dry_run_by_default_and_requires_exact_confirmation(self):
        previous_path = os.environ.get("SRE_AGENT_DB_PATH")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SRE_AGENT_DB_PATH"] = str(Path(temp_dir) / "retention.sqlite3")
            try:
                init_db()
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO logs (service, timestamp, level, message)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("retention-test", "2000-01-01 00:00:00", "INFO", "expired"),
                )
                cur.execute(
                    """
                    INSERT INTO usage_events (
                        id, workspace_id, metric, quantity, occurred_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), "default", "api_request", 1, "2000-01-01T00:00:00+00:00"),
                )
                conn.commit()
                conn.close()

                now = datetime(2026, 7, 19, tzinfo=timezone.utc)
                preview = retention_preview(now)
                self.assertEqual(preview["mode"], "dry_run")
                self.assertEqual(preview["candidates"]["logs"], 1)
                self.assertEqual(preview["candidates"]["usage_events"], 1)
                with self.assertRaises(PermissionError):
                    purge_retained_data(confirmation="PURGE:wrong", now=now)

                applied = purge_retained_data(confirmation="PURGE:default", now=now)
                self.assertEqual(applied["mode"], "applied")
                self.assertEqual(applied["deleted"]["logs"], 1)
                self.assertEqual(applied["deleted"]["usage_events"], 1)
                after = retention_preview(now)
                self.assertEqual(after["candidate_total"], 0)
            finally:
                if previous_path is None:
                    os.environ.pop("SRE_AGENT_DB_PATH", None)
                else:
                    os.environ["SRE_AGENT_DB_PATH"] = previous_path

    def test_postgres_sql_compatibility_translation(self):
        self.assertEqual(_postgres_sql("BEGIN IMMEDIATE"), "BEGIN")
        translated = _postgres_sql(
            "INSERT OR IGNORE INTO sample(value, note) VALUES (?, '?')"
        )
        self.assertIn("INSERT INTO sample", translated)
        self.assertIn("VALUES (%s, '?')", translated)
        self.assertTrue(translated.endswith("ON CONFLICT DO NOTHING"))
        self.assertIn(
            "BIGSERIAL PRIMARY KEY",
            _postgres_sql("id INTEGER PRIMARY KEY AUTOINCREMENT"),
        )
        self.assertIn(
            "information_schema.columns",
            _postgres_sql("PRAGMA table_info(task_runs)"),
        )

    def test_postgres_cursor_translates_batch_statements(self):
        class RecordingCursor:
            def executemany(self, sql, params_seq):
                self.sql = sql
                self.params = list(params_seq)

        raw_cursor = RecordingCursor()
        cursor = _PostgresCursor(raw_cursor)
        result = cursor.executemany(
            "INSERT OR IGNORE INTO sample(value) VALUES (?)",
            [("first",), ("second",)],
        )

        self.assertIs(result, cursor)
        self.assertEqual(raw_cursor.params, [("first",), ("second",)])
        self.assertIn("VALUES (%s)", raw_cursor.sql)
        self.assertTrue(raw_cursor.sql.endswith("ON CONFLICT DO NOTHING"))

    def test_smoke_response_headers_are_case_insensitive(self):
        normalized = normalize_headers(
            {
                "X-Trace-ID": "a" * 32,
                "TraceParent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
            }
        )

        self.assertEqual(normalized["x-trace-id"], "a" * 32)
        self.assertTrue(normalized["traceparent"].startswith("00-"))

    def test_schema_migrations_are_current_and_idempotent(self):
        init_db()
        init_db()
        status = get_schema_status()
        self.assertTrue(status["compatible"])
        self.assertEqual(status["applied_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(status["current_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            [item["version"] for item in status["migrations"]],
            list(range(1, CURRENT_SCHEMA_VERSION + 1)),
        )

    @unittest.skipIf(is_postgres_database(), "isolated SQLite path is used for audit ledger test")
    def test_audit_ledger_detects_tampering(self):
        previous_path = os.environ.get("SRE_AGENT_DB_PATH")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SRE_AGENT_DB_PATH"] = str(Path(temp_dir) / "audit.sqlite3")
            try:
                init_db()
                save_execution_audit(
                    "deploy_requested",
                    "payment-service",
                    "api",
                    "pending",
                    reason="中文审计原因",
                    actor="api-key:operator:test",
                    change_request_id="change-audit-ledger-test",
                )
                verified = verify_audit_ledger()
                self.assertTrue(verified["valid"])
                self.assertEqual(verified["entry_count"], 1)
                self.assertIsNotNone(verified["head_hash"])

                purged = purge_retained_data(
                    confirmation="PURGE:default",
                    now=datetime.now(timezone.utc) + timedelta(days=3000),
                )
                self.assertEqual(purged["deleted"]["audit_ledger_entries"], 1)
                checkpointed = verify_audit_ledger()
                self.assertTrue(checkpointed["valid"])
                self.assertEqual(checkpointed["entry_count"], 0)
                self.assertEqual(checkpointed["pruned_entry_count"], 1)

                save_execution_audit(
                    "deploy_completed",
                    "payment-service",
                    "worker",
                    "completed",
                    actor="worker:test",
                    change_request_id="change-audit-ledger-test",
                )
                continued = verify_audit_ledger()
                self.assertTrue(continued["valid"])
                self.assertEqual(continued["entry_count"], 1)
                self.assertEqual(continued["pruned_entry_count"], 1)

                conn = get_conn()
                conn.cursor().execute(
                    "UPDATE audit_ledger SET status = ?",
                    ("tampered",),
                )
                conn.commit()
                conn.close()

                tampered = verify_audit_ledger()
                self.assertFalse(tampered["valid"])
                self.assertEqual(tampered["failure"]["reason"], "payload_mismatch")
            finally:
                if previous_path is None:
                    os.environ.pop("SRE_AGENT_DB_PATH", None)
                else:
                    os.environ["SRE_AGENT_DB_PATH"] = previous_path

    @unittest.skipIf(is_postgres_database(), "isolated SQLite path is used for worker readiness test")
    def test_queued_readiness_requires_fresh_worker_heartbeat(self):
        previous_path = os.environ.get("SRE_AGENT_DB_PATH")
        os.environ["SRE_CHANGE_EXECUTION_MODE"] = "queued"
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SRE_AGENT_DB_PATH"] = str(Path(temp_dir) / "worker.sqlite3")
            try:
                with TestClient(app) as client:
                    missing = client.get("/health/ready")
                    self.assertEqual(missing.status_code, 503)
                    self.assertFalse(missing.json()["checks"]["change_worker"])

                    started_at = datetime.now(timezone.utc).isoformat()
                    touch_worker_heartbeat(
                        "worker-readiness-test",
                        hostname="test-host",
                        process_id=12345,
                        status="idle",
                        started_at=started_at,
                    )
                    healthy = worker_heartbeat_status()
                    self.assertTrue(healthy["healthy"])
                    self.assertEqual(healthy["active_count"], 1)
                    ready = client.get("/health/ready")
                    self.assertEqual(ready.status_code, 200)
                    self.assertTrue(ready.json()["checks"]["change_worker"])

                    conn = get_conn()
                    conn.cursor().execute(
                        "UPDATE worker_heartbeats SET last_seen_at = ?",
                        ("2000-01-01T00:00:00+00:00",),
                    )
                    conn.commit()
                    conn.close()
                    stale = client.get("/health/ready")
                    self.assertEqual(stale.status_code, 503)
                    self.assertFalse(stale.json()["checks"]["change_worker"])
            finally:
                os.environ.pop("SRE_CHANGE_EXECUTION_MODE", None)
                if previous_path is None:
                    os.environ.pop("SRE_AGENT_DB_PATH", None)
                else:
                    os.environ["SRE_AGENT_DB_PATH"] = previous_path

    @unittest.skipIf(is_postgres_database(), "isolated SQLite path is used for entitlement test")
    def test_plan_entitlements_cap_keys_and_gate_production_writes(self):
        previous_path = os.environ.get("SRE_AGENT_DB_PATH")
        previous_plan = os.environ.get("SRE_PLAN")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SRE_AGENT_DB_PATH"] = str(Path(temp_dir) / "entitlements.sqlite3")
            os.environ["SRE_PLAN"] = "trial"
            try:
                init_db()
                seed_data()
                self.assertFalse(get_plan_entitlements()["production_writes"])
                for index in range(3):
                    issue_workspace_api_key(f"trial-key-{index}", "viewer")
                with self.assertRaises(PlanEntitlementError):
                    issue_workspace_api_key("trial-key-over-limit", "viewer")

                os.environ["SRE_ENVIRONMENT"] = "production"
                os.environ["SRE_PRODUCTION_WRITE_ENABLED"] = "true"
                pending = deploy(
                    DeployRequest(
                        service_name="payment-service",
                        new_version="v-entitlement-gate",
                    )
                )
                with self.assertRaises(HTTPException) as denied:
                    confirm_change(
                        pending["change_request_id"],
                        ChangeConfirmRequest(),
                    )
                self.assertEqual(denied.exception.status_code, 402)
            finally:
                os.environ["SRE_ENVIRONMENT"] = "development"
                os.environ.pop("SRE_PRODUCTION_WRITE_ENABLED", None)
                if previous_plan is None:
                    os.environ.pop("SRE_PLAN", None)
                else:
                    os.environ["SRE_PLAN"] = previous_plan
                if previous_path is None:
                    os.environ.pop("SRE_AGENT_DB_PATH", None)
                else:
                    os.environ["SRE_AGENT_DB_PATH"] = previous_path

    def test_outbound_url_guard_blocks_metadata_and_private_targets(self):
        with self.assertRaises(UnsafeOutboundUrl):
            validate_outbound_url("http://169.254.169.254/latest/meta-data")
        with self.assertRaises(UnsafeOutboundUrl):
            validate_outbound_url("http://127.0.0.1:9000/private")

        os.environ["SRE_OUTBOUND_HOST_ALLOWLIST"] += ",127.0.0.1"
        self.assertEqual(
            validate_outbound_url("http://127.0.0.1:9000/health"),
            "http://127.0.0.1:9000/health",
        )

    def test_api_key_roles_protect_read_and_write_routes(self):
        os.environ["SRE_AUTH_ENABLED"] = "true"
        os.environ["SRE_VIEWER_API_KEY"] = "viewer-test-key"
        os.environ["SRE_OPERATOR_API_KEY"] = "operator-test-key"
        os.environ["SRE_ADMIN_API_KEY"] = "admin-test-key"
        try:
            with TestClient(app) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/services/").status_code, 401)

                viewer_headers = {"X-SRE-API-Key": "viewer-test-key"}
                self.assertEqual(client.get("/services/", headers=viewer_headers).status_code, 200)
                viewer_deploy = client.post(
                    "/deploy",
                    headers=viewer_headers,
                    json={"service_name": "payment-service", "new_version": "v7.7.7", "dry_run": True},
                )
                self.assertEqual(viewer_deploy.status_code, 403)

                operator_deploy = client.post(
                    "/deploy",
                    headers={"X-SRE-API-Key": "operator-test-key"},
                    json={"service_name": "payment-service", "new_version": "v7.7.7", "dry_run": True},
                )
                self.assertEqual(operator_deploy.status_code, 200)

                auth_info = client.get("/auth/me", headers={"Authorization": "Bearer admin-test-key"})
                self.assertEqual(auth_info.status_code, 200)
                self.assertEqual(auth_info.json()["role"], "admin")
        finally:
            os.environ["SRE_AUTH_ENABLED"] = "false"
            os.environ.pop("SRE_VIEWER_API_KEY", None)
            os.environ.pop("SRE_OPERATOR_API_KEY", None)
            os.environ.pop("SRE_ADMIN_API_KEY", None)

    def test_audit_records_requester_and_approver_identity(self):
        os.environ["SRE_AUTH_ENABLED"] = "true"
        os.environ["SRE_OPERATOR_API_KEY"] = "operator-audit-key"
        os.environ["SRE_ADMIN_API_KEY"] = "admin-audit-key"
        operator_headers = {"X-SRE-API-Key": "operator-audit-key"}
        admin_headers = {"X-SRE-API-Key": "admin-audit-key"}
        try:
            with TestClient(app) as client:
                submitted = client.post(
                    "/deploy",
                    headers=operator_headers,
                    json={
                        "service_name": "payment-service",
                        "new_version": "v-audit-test",
                    },
                )
                self.assertEqual(submitted.status_code, 200)
                change_request_id = submitted.json()["change_request_id"]

                confirmed = client.post(
                    f"/changes/{change_request_id}/confirm",
                    headers=operator_headers,
                    json={"dry_run": True},
                )
                self.assertEqual(confirmed.status_code, 200)

                detail = client.get(
                    f"/changes/{change_request_id}",
                    headers=operator_headers,
                ).json()["change_request"]
                self.assertTrue(detail["requested_by"].startswith("api-key:operator:"))
                self.assertEqual(detail["approved_by"], detail["requested_by"])

                audit_response = client.get(
                    "/audit/executions",
                    headers=admin_headers,
                    params={"service_name": "payment-service", "limit": 100},
                )
                self.assertEqual(audit_response.status_code, 200)
                matching = [
                    item
                    for item in audit_response.json()["audits"]
                    if item["change_request_id"] == change_request_id
                ]
                self.assertGreaterEqual(len(matching), 2)
                self.assertTrue(
                    all(item["actor"].startswith("api-key:operator:") for item in matching)
                )
        finally:
            os.environ["SRE_AUTH_ENABLED"] = "false"
            os.environ.pop("SRE_OPERATOR_API_KEY", None)
            os.environ.pop("SRE_ADMIN_API_KEY", None)

    def test_internal_metrics_exposes_success_rate_and_response_time(self):
        with TestClient(app) as client:
            client.get("/services/")
            resp = client.get("/internal/metrics")

        self.assertEqual(resp.status_code, 200)
        metrics = resp.json()["metrics"]
        self.assertGreaterEqual(metrics["request_count"], 1)
        self.assertIn("success_rate_pct", metrics)
        self.assertIn("avg_response_time_ms", metrics)
        self.assertIn("p95_response_time_ms", metrics)
        self.assertIn("change_queue", metrics)
        self.assertIn("queued", metrics["change_queue"])
        self.assertIn("change_workers", metrics)
        self.assertIn("active_count", metrics["change_workers"])
        self.assertIn("incidents", metrics)
        self.assertIn("open", metrics["incidents"])

    def test_incident_correlation_deduplicates_and_tracks_lifecycle(self):
        unique = uuid.uuid4().hex
        correlated = correlate_alerts(
            [
                {
                    "id": f"alert-a-{unique}",
                    "service": "inventory-service",
                    "severity": "warning",
                    "title": f"Inventory latency {unique} 123 ms",
                    "message": "latency is elevated",
                    "created_at": "2026-07-19T10:00:00Z",
                },
                {
                    "id": f"alert-b-{unique}",
                    "service": "inventory-service",
                    "severity": "critical",
                    "title": f"Inventory latency {unique} 456 ms",
                    "message": "latency is critical",
                    "created_at": "2026-07-19T10:01:00Z",
                },
            ],
            actor="regression-correlator",
            source="regression",
        )
        self.assertEqual(len(correlated), 1)
        incident_id = correlated[0]["id"]
        detail = incident_detail(incident_id)["incident"]
        self.assertEqual(detail["alert_count"], 2)
        self.assertEqual(detail["severity"], "critical")
        self.assertEqual(len(detail["alerts"]), 2)
        self.assertEqual(len(detail["events"]), 2)

        duplicate = correlate_alerts(
            [
                {
                    "id": f"alert-a-{unique}",
                    "service": "inventory-service",
                    "severity": "warning",
                    "title": f"Inventory latency {unique} 123 ms",
                }
            ],
            actor="regression-correlator",
            source="regression",
        )
        self.assertEqual(duplicate, [])
        self.assertEqual(incident_detail(incident_id)["incident"]["alert_count"], 2)

        updated = change_incident(
            incident_id,
            IncidentUpdateRequest(
                status="investigating",
                owner="oncall@example.com",
                summary="Investigating inventory latency",
            ),
        )["incident"]
        self.assertEqual(updated["status"], "investigating")
        self.assertEqual(updated["owner"], "oncall@example.com")
        self.assertEqual(updated["events"][-1]["event_type"], "incident_updated")

        listed = incidents(status="investigating", service_name="inventory-service")
        self.assertTrue(any(item["id"] == incident_id for item in listed["incidents"]))

        with self.assertRaises(HTTPException) as invalid_transition:
            change_incident(
                incident_id,
                IncidentUpdateRequest(status="open"),
            )
        self.assertEqual(invalid_transition.exception.status_code, 409)

    def test_analyzer_enforces_evidence_item_text_and_total_budgets(self):
        analyzer = ServiceHealthAnalyzer(
            EvidenceBudget(
                max_items_per_source=2,
                max_text_chars=20,
                max_total_bytes=2000,
            )
        )
        long_text = "x" * 500
        with patch(
            "backend.analyzers.service_health.get_recent_alerts",
            return_value=[
                {"id": index, "message": long_text}
                for index in range(5)
            ],
        ), patch(
            "backend.analyzers.service_health.get_service_status",
            return_value={"status": "running", "details": long_text},
        ), patch(
            "backend.analyzers.service_health.get_service_metrics",
            return_value={"error_rate": 1.2},
        ), patch(
            "backend.analyzers.service_health.get_recent_logs",
            return_value=[{"message": long_text} for _ in range(5)],
        ), patch(
            "backend.analyzers.service_health.get_recent_deploy_context",
            return_value=[],
        ), patch(
            "backend.analyzers.service_health.get_external_k8s_observability",
            return_value={f"key-{index}": long_text for index in range(100)},
        ):
            result = analyzer.run("inventory-service")

        self.assertEqual(len(result.evidence["alerts"]), 3)
        self.assertEqual(result.evidence["alerts"][-1]["_truncated_items"], 3)
        self.assertIn("[truncated]", result.evidence["status"]["details"])
        self.assertLessEqual(result.budget["actual_total_bytes"], 2000)
        self.assertIn("k8s_observability", result.budget["truncated_sources"])
        self.assertEqual(result.steps[-1]["action"], "evidence_budget")

    def test_prometheus_metrics_endpoint_returns_text_export(self):
        with TestClient(app) as client:
            client.get("/services/")
            resp = client.get("/metrics")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("sre_agent_request_total", resp.text)
        self.assertIn("sre_agent_success_rate_pct", resp.text)
        self.assertIn("sre_agent_p95_response_time_ms", resp.text)
        self.assertIn('sre_agent_change_jobs{status="queued"}', resp.text)
        self.assertIn('sre_agent_change_workers{state="active"}', resp.text)

    def test_metrics_require_auth_and_responses_have_security_headers(self):
        os.environ["SRE_AUTH_ENABLED"] = "true"
        os.environ["SRE_VIEWER_API_KEY"] = "metrics-viewer-key"
        try:
            with TestClient(app) as client:
                root_response = client.get("/")
                self.assertEqual(root_response.headers["x-frame-options"], "DENY")
                self.assertEqual(
                    root_response.headers["x-content-type-options"],
                    "nosniff",
                )
                self.assertIn("frame-ancestors 'none'", root_response.headers["content-security-policy"])
                self.assertEqual(len(root_response.headers["x-trace-id"]), 32)
                self.assertTrue(root_response.headers["traceparent"].startswith("00-"))

                self.assertEqual(client.get("/metrics").status_code, 401)
                authorized = client.get(
                    "/metrics",
                    headers={"X-SRE-API-Key": "metrics-viewer-key"},
                )
                self.assertEqual(authorized.status_code, 200)
        finally:
            os.environ["SRE_AUTH_ENABLED"] = "false"
            os.environ.pop("SRE_VIEWER_API_KEY", None)

    def test_w3c_trace_context_is_validated_and_propagated(self):
        trace_id = "1" * 32
        incoming = f"00-{trace_id}-{'2' * 16}-01"
        with TestClient(app) as client:
            propagated = client.get("/", headers={"traceparent": incoming})
            generated = client.get(
                "/",
                headers={"traceparent": f"00-{'0' * 32}-{'2' * 16}-01"},
            )
        self.assertEqual(propagated.headers["x-trace-id"], trace_id)
        self.assertTrue(propagated.headers["traceparent"].startswith(f"00-{trace_id}-"))
        self.assertNotEqual(generated.headers["x-trace-id"], "0" * 32)
        self.assertEqual(len(generated.headers["x-trace-id"]), 32)

    def test_workspace_api_key_lifecycle_and_usage_metering(self):
        os.environ["SRE_AUTH_ENABLED"] = "true"
        os.environ["SRE_ADMIN_API_KEY"] = "workspace-bootstrap-admin"
        admin_headers = {"X-SRE-API-Key": "workspace-bootstrap-admin"}
        created_key_id = None
        try:
            with TestClient(app) as client:
                baseline = client.get("/billing/usage", headers=admin_headers)
                self.assertEqual(baseline.status_code, 200)
                baseline_requests = baseline.json()["requests_used"]
                baseline_chats = baseline.json()["usage"].get("chat_request", 0)

                created = client.post(
                    "/workspace/api-keys",
                    headers=admin_headers,
                    json={"name": "regression viewer", "role": "viewer"},
                )
                self.assertEqual(created.status_code, 201)
                issued = created.json()["api_key"]
                created_key_id = issued["id"]
                raw_key = issued["api_key"]
                self.assertTrue(raw_key.startswith("sre_live_"))

                workspace_headers = {"Authorization": f"Bearer {raw_key}"}
                identity = client.get("/auth/me", headers=workspace_headers)
                self.assertEqual(identity.status_code, 200)
                self.assertEqual(identity.json()["auth_source"], "workspace_api_key")
                self.assertEqual(identity.json()["workspace_id"], "default")

                services_response = client.get("/services/", headers=workspace_headers)
                self.assertEqual(services_response.status_code, 200)

                chat_response = client.post(
                    "/chat",
                    headers=workspace_headers,
                    json={"message": "payment-service status"},
                )
                self.assertEqual(chat_response.status_code, 200)

                usage = client.get("/billing/usage", headers=workspace_headers)
                self.assertEqual(usage.status_code, 200)
                self.assertGreaterEqual(usage.json()["requests_used"], baseline_requests + 3)
                self.assertGreaterEqual(
                    usage.json()["usage"].get("chat_request", 0),
                    baseline_chats + 1,
                )
                self.assertIn("llm_cost_usd", usage.json())

                self.assertEqual(
                    client.get("/billing/usage.csv", headers=workspace_headers).status_code,
                    403,
                )
                usage_csv = client.get("/billing/usage.csv", headers=admin_headers)
                self.assertEqual(usage_csv.status_code, 200)
                self.assertIn("text/csv", usage_csv.headers["content-type"])
                self.assertIn("attachment;", usage_csv.headers["content-disposition"])
                self.assertIn("occurred_at,workspace_id,metric,quantity", usage_csv.text)
                self.assertIn("chat_request", usage_csv.text)
                self.assertGreater(int(usage_csv.headers["x-usage-event-count"]), 0)
                self.assertEqual(
                    client.get("/billing/usage?month=2026-13", headers=admin_headers).status_code,
                    400,
                )

                keys = client.get("/workspace/api-keys", headers=admin_headers)
                self.assertEqual(keys.status_code, 200)
                stored = next(item for item in keys.json()["api_keys"] if item["id"] == created_key_id)
                self.assertNotIn("api_key", stored)
                self.assertNotIn("key_hash", stored)

                revoked = client.delete(
                    f"/workspace/api-keys/{created_key_id}",
                    headers=admin_headers,
                )
                self.assertEqual(revoked.status_code, 200)
                self.assertEqual(client.get("/auth/me", headers=workspace_headers).status_code, 401)
        finally:
            if created_key_id:
                revoke_workspace_api_key(created_key_id)
            os.environ["SRE_AUTH_ENABLED"] = "false"
            os.environ.pop("SRE_ADMIN_API_KEY", None)

    def test_pilot_value_report_is_idempotent_and_admin_only(self):
        os.environ["SRE_AUTH_ENABLED"] = "true"
        os.environ["SRE_ADMIN_API_KEY"] = "pilot-report-admin"
        os.environ["SRE_VIEWER_API_KEY"] = "pilot-report-viewer"
        os.environ["SRE_PLAN_PRICE_USD_MONTHLY"] = "1000"
        os.environ["SRE_INFRA_COST_USD_MONTHLY"] = "100"
        os.environ["SRE_CUSTOMER_HOURLY_COST_USD"] = "100"
        os.environ["SRE_SUPPORT_HOURLY_COST_USD"] = "50"
        admin_headers = {"X-SRE-API-Key": "pilot-report-admin"}
        viewer_headers = {"X-SRE-API-Key": "pilot-report-viewer"}
        payload = {
            "idempotency_key": "regression-pilot-value-v1",
            "category": "diagnosis",
            "service_name": "payment-service",
            "baseline_minutes": 120,
            "actual_minutes": 30,
            "support_minutes": 15,
            "recommendation_accepted": True,
            "successful": True,
            "notes": "Regression fixture for the paid-pilot evidence loop.",
            "occurred_at": "2025-01-15T12:00:00+00:00",
        }
        try:
            with TestClient(app) as client:
                created = client.post(
                    "/billing/pilot-outcomes",
                    headers=admin_headers,
                    json=payload,
                )
                self.assertEqual(created.status_code, 201)
                outcome_id = created.json()["outcome"]["id"]

                replay = client.post(
                    "/billing/pilot-outcomes",
                    headers=admin_headers,
                    json=payload,
                )
                self.assertEqual(replay.status_code, 201)
                self.assertFalse(replay.json()["created"])
                self.assertTrue(replay.json()["idempotent_replay"])
                self.assertEqual(replay.json()["outcome"]["id"], outcome_id)

                conflicting_payload = dict(payload, actual_minutes=31)
                conflict = client.post(
                    "/billing/pilot-outcomes",
                    headers=admin_headers,
                    json=conflicting_payload,
                )
                self.assertEqual(conflict.status_code, 409)

                query = "start_date=2025-01-15&end_date=2025-01-15"
                report_response = client.get(
                    f"/billing/value-report?{query}",
                    headers=admin_headers,
                )
                self.assertEqual(report_response.status_code, 200)
                report = report_response.json()
                self.assertEqual(report["outcomes"]["recorded"], 1)
                self.assertEqual(report["outcomes"]["net_minutes_saved"], 90)
                self.assertEqual(report["outcomes"]["recommendation_acceptance_pct"], 100)
                self.assertEqual(report["outcomes"]["success_rate_pct"], 100)
                self.assertEqual(report["outcomes"]["support_minutes"], 15)
                self.assertEqual(report["economics"]["customer_labor_value_usd"], 150)
                self.assertGreater(report["economics"]["gross_margin_usd"], 0)
                self.assertTrue(report["evidence_quality"]["has_cost_assumptions"])

                exported = client.get(
                    f"/billing/value-report.csv?{query}",
                    headers=admin_headers,
                )
                self.assertEqual(exported.status_code, 200)
                self.assertIn("text/csv", exported.headers["content-type"])
                self.assertIn("outcomes_recorded", exported.text)
                self.assertEqual(exported.headers["x-pilot-outcome-count"], "1")

                self.assertEqual(
                    client.get("/billing/value-report", headers=viewer_headers).status_code,
                    403,
                )
                self.assertEqual(
                    client.post(
                        "/billing/pilot-outcomes",
                        headers=viewer_headers,
                        json=payload,
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.get(
                        "/billing/value-report?start_date=2025-02-01&end_date=2025-01-01",
                        headers=admin_headers,
                    ).status_code,
                    400,
                )
        finally:
            os.environ["SRE_AUTH_ENABLED"] = "false"
            os.environ.pop("SRE_ADMIN_API_KEY", None)
            os.environ.pop("SRE_VIEWER_API_KEY", None)
            os.environ.pop("SRE_PLAN_PRICE_USD_MONTHLY", None)
            os.environ.pop("SRE_INFRA_COST_USD_MONTHLY", None)
            os.environ.pop("SRE_CUSTOMER_HOURLY_COST_USD", None)
            os.environ.pop("SRE_SUPPORT_HOURLY_COST_USD", None)

    def test_subscription_expiry_blocks_operations_without_blocking_recovery(self):
        tracked_env = {
            name: os.environ.get(name)
            for name in (
                "SRE_PLAN",
                "SRE_SUBSCRIPTION_STATUS",
                "SRE_TRIAL_DAYS",
                "SRE_TRIAL_ENDS_AT",
                "SRE_CURRENT_PERIOD_END",
                "SRE_MONTHLY_REQUEST_LIMIT",
                "SRE_ENVIRONMENT",
                "SRE_AUTH_ENABLED",
                "SRE_ADMIN_API_KEY",
            )
        }
        original_workspace = get_workspace()
        created_key_id = None
        try:
            os.environ["SRE_TRIAL_DAYS"] = "0"
            with self.assertRaises(RuntimeError):
                init_db()
            os.environ["SRE_TRIAL_DAYS"] = "14"
            os.environ["SRE_PLAN"] = "trial"
            os.environ["SRE_SUBSCRIPTION_STATUS"] = "trialing"
            os.environ["SRE_TRIAL_ENDS_AT"] = "2099-01-01T00:00:00Z"
            os.environ["SRE_MONTHLY_REQUEST_LIMIT"] = "1000"
            init_db()
            first_trial_end = get_subscription_status()["trial_ends_at"]
            os.environ.pop("SRE_TRIAL_ENDS_AT", None)
            init_db()
            self.assertEqual(get_subscription_status()["trial_ends_at"], first_trial_end)

            issued = issue_workspace_api_key("subscription-regression", "viewer")
            created_key_id = issued["id"]
            workspace_headers = {"Authorization": f"Bearer {issued['api_key']}"}
            os.environ["SRE_AUTH_ENABLED"] = "true"
            os.environ["SRE_ADMIN_API_KEY"] = "subscription-bootstrap-admin"
            bootstrap_headers = {"X-SRE-API-Key": "subscription-bootstrap-admin"}

            with TestClient(app) as client:
                os.environ["SRE_SUBSCRIPTION_STATUS"] = "expired"
                os.environ["SRE_TRIAL_ENDS_AT"] = "2025-01-01T00:00:00Z"
                init_db()

                workspace_denied = client.get("/services/", headers=workspace_headers)
                self.assertEqual(workspace_denied.status_code, 402)
                self.assertEqual(
                    workspace_denied.json()["detail"]["error"],
                    "subscription_inactive",
                )
                bootstrap_denied = client.get("/services/", headers=bootstrap_headers)
                self.assertEqual(bootstrap_denied.status_code, 402)

                subscription = client.get(
                    "/billing/subscription",
                    headers=workspace_headers,
                )
                self.assertEqual(subscription.status_code, 200)
                self.assertEqual(
                    subscription.json()["subscription"]["effective_status"],
                    "expired",
                )
                self.assertFalse(
                    subscription.json()["subscription"]["access_allowed"]
                )
                self.assertGreaterEqual(len(subscription.json()["events"]), 1)
                self.assertEqual(
                    client.get("/workspace", headers=workspace_headers).status_code,
                    200,
                )
                self.assertEqual(
                    client.get("/billing/usage", headers=workspace_headers).status_code,
                    200,
                )
                self.assertEqual(
                    client.post(
                        "/workspace/api-keys",
                        headers=bootstrap_headers,
                        json={"name": "blocked-expired-key", "role": "viewer"},
                    ).status_code,
                    402,
                )

                os.environ["SRE_PLAN"] = "team"
                os.environ["SRE_SUBSCRIPTION_STATUS"] = "active"
                os.environ.pop("SRE_TRIAL_ENDS_AT", None)
                os.environ["SRE_MONTHLY_REQUEST_LIMIT"] = "100000"
                init_db()
                self.assertEqual(
                    client.get("/services/", headers=workspace_headers).status_code,
                    200,
                )

                os.environ["SRE_SUBSCRIPTION_STATUS"] = "past_due"
                os.environ["SRE_CURRENT_PERIOD_END"] = "2099-02-01T00:00:00Z"
                init_db()
                grace = get_subscription_status()
                self.assertEqual(grace["effective_status"], "grace_period")
                self.assertTrue(grace["access_allowed"])
                self.assertEqual(
                    client.get("/services/", headers=workspace_headers).status_code,
                    200,
                )
                os.environ["SRE_SUBSCRIPTION_STATUS"] = "active"
                init_db()

                os.environ["SRE_ENVIRONMENT"] = "production"
                self.assertEqual(
                    client.get("/services/", headers=bootstrap_headers).status_code,
                    403,
                )
                self.assertEqual(
                    client.get(
                        "/billing/subscription",
                        headers=bootstrap_headers,
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    client.get("/services/", headers=workspace_headers).status_code,
                    200,
                )
        finally:
            if created_key_id:
                revoke_workspace_api_key(created_key_id)
            for name, value in tracked_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            if original_workspace:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE workspaces
                    SET name = ?, plan = ?, status = ?, subscription_status = ?,
                        trial_ends_at = ?, current_period_end = ?,
                        subscription_updated_at = ?, monthly_request_limit = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        original_workspace["name"],
                        original_workspace["plan"],
                        original_workspace["status"],
                        original_workspace.get("subscription_status"),
                        original_workspace.get("trial_ends_at"),
                        original_workspace.get("current_period_end"),
                        original_workspace.get("subscription_updated_at"),
                        original_workspace["monthly_request_limit"],
                        original_workspace["updated_at"],
                        original_workspace["id"],
                    ),
                )
                conn.commit()
                conn.close()

    def test_production_readiness_fails_closed(self):
        os.environ["SRE_ENVIRONMENT"] = "production"
        os.environ["SRE_AUTH_ENABLED"] = "false"
        os.environ["EXECUTION_GUARD_ENABLED"] = "false"
        os.environ["SRE_ALLOW_INSECURE_DB_SECRETS"] = "true"
        try:
            with TestClient(app) as client:
                not_ready = client.get("/health/ready")
                self.assertEqual(not_ready.status_code, 503)
                self.assertEqual(not_ready.json()["status"], "not_ready")

                os.environ["SRE_AUTH_ENABLED"] = "true"
                os.environ["SRE_ADMIN_API_KEY"] = "readiness-admin-key"
                os.environ["EXECUTION_GUARD_ENABLED"] = "true"
                os.environ["EXECUTION_GUARD_TOKEN"] = "readiness-guard-key"
                os.environ["SRE_ALLOW_INSECURE_DB_SECRETS"] = "false"

                database_readiness = client.get("/health/ready")
                if is_postgres_database():
                    self.assertTrue(
                        database_readiness.json()["checks"]["production_database"]
                    )
                    self.assertFalse(
                        database_readiness.json()["checks"]["real_data_source"]
                    )
                else:
                    self.assertEqual(database_readiness.status_code, 503)
                    self.assertFalse(
                        database_readiness.json()["checks"]["production_database"]
                    )

                os.environ["SRE_ALLOW_PRODUCTION_SQLITE"] = "true"
                os.environ["SRE_REQUIRE_REAL_DATA_SOURCE"] = "false"
                ready = client.get("/health/ready")
                self.assertEqual(ready.status_code, 200)
                self.assertEqual(ready.json()["status"], "ready")

                os.environ["SRE_PRODUCTION_WRITE_ENABLED"] = "true"
                unsafe_executor = client.get("/health/ready")
                self.assertEqual(unsafe_executor.status_code, 503)
                self.assertFalse(unsafe_executor.json()["checks"]["production_executor"])

                os.environ["SRE_CHANGE_EXECUTOR"] = "webhook"
                os.environ["SRE_CHANGE_EXECUTOR_WEBHOOK_URL"] = "https://executor.example.com/changes"
                os.environ["SRE_CHANGE_EXECUTOR_TOKEN"] = "readiness-executor-token"
                unentitled = client.get("/health/ready")
                self.assertEqual(unentitled.status_code, 503)
                self.assertFalse(
                    unentitled.json()["checks"]["production_write_entitlement"]
                )

                os.environ["SRE_PLAN"] = "team"
                init_db()
                safe_executor = client.get("/health/ready")
                self.assertEqual(safe_executor.status_code, 200)
                self.assertTrue(safe_executor.json()["checks"]["production_executor"])
                self.assertTrue(
                    safe_executor.json()["checks"]["production_write_entitlement"]
                )
        finally:
            os.environ["SRE_ENVIRONMENT"] = "development"
            os.environ["SRE_AUTH_ENABLED"] = "false"
            os.environ["EXECUTION_GUARD_ENABLED"] = "false"
            os.environ.pop("SRE_ADMIN_API_KEY", None)
            os.environ.pop("EXECUTION_GUARD_TOKEN", None)
            os.environ.pop("SRE_ALLOW_INSECURE_DB_SECRETS", None)
            os.environ.pop("SRE_ALLOW_PRODUCTION_SQLITE", None)
            os.environ.pop("SRE_REQUIRE_REAL_DATA_SOURCE", None)
            os.environ.pop("SRE_PRODUCTION_WRITE_ENABLED", None)
            os.environ.pop("SRE_CHANGE_EXECUTOR", None)
            os.environ.pop("SRE_CHANGE_EXECUTOR_WEBHOOK_URL", None)
            os.environ.pop("SRE_CHANGE_EXECUTOR_TOKEN", None)
            os.environ["SRE_PLAN"] = "trial"
            init_db()
            os.environ.pop("SRE_PLAN", None)

    def test_production_write_is_disabled_by_default_but_dry_run_remains_available(self):
        os.environ["SRE_ENVIRONMENT"] = "production"
        os.environ["SRE_PRODUCTION_WRITE_ENABLED"] = "false"
        pending = deploy(
            DeployRequest(
                service_name="payment-service",
                new_version="v-production-policy",
            )
        )
        try:
            with self.assertRaises(HTTPException) as denied:
                confirm_change(
                    pending["change_request_id"],
                    ChangeConfirmRequest(),
                    x_guard_token=None,
                )
            self.assertEqual(denied.exception.status_code, 403)
            preview = confirm_change(
                pending["change_request_id"],
                ChangeConfirmRequest(dry_run=True),
                x_guard_token=None,
            )
            self.assertEqual(preview["execution_mode"], "dry_run")
        finally:
            os.environ["SRE_ENVIRONMENT"] = "development"
            os.environ.pop("SRE_PRODUCTION_WRITE_ENABLED", None)

    @unittest.skipIf(is_postgres_database(), "isolated SQLite path is used for seed policy test")
    def test_production_never_seeds_demo_data_without_explicit_override(self):
        previous_path = os.environ.get("SRE_AGENT_DB_PATH")
        previous_environment = os.environ.get("SRE_ENVIRONMENT")
        previous_seed = os.environ.get("SRE_SEED_DEMO_DATA")
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SRE_AGENT_DB_PATH"] = str(Path(temp_dir) / "production.sqlite3")
            os.environ["SRE_ENVIRONMENT"] = "production"
            os.environ.pop("SRE_SEED_DEMO_DATA", None)
            try:
                init_db()
                seed_data()
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) AS count FROM services")
                self.assertEqual(cur.fetchone()["count"], 0)
                conn.close()

                os.environ["SRE_SEED_DEMO_DATA"] = "true"
                seed_data()
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) AS count FROM services")
                self.assertEqual(cur.fetchone()["count"], 2)
                conn.close()
            finally:
                if previous_path is None:
                    os.environ.pop("SRE_AGENT_DB_PATH", None)
                else:
                    os.environ["SRE_AGENT_DB_PATH"] = previous_path
                if previous_environment is None:
                    os.environ.pop("SRE_ENVIRONMENT", None)
                else:
                    os.environ["SRE_ENVIRONMENT"] = previous_environment
                if previous_seed is None:
                    os.environ.pop("SRE_SEED_DEMO_DATA", None)
                else:
                    os.environ["SRE_SEED_DEMO_DATA"] = previous_seed

    def test_real_data_source_readiness_status_rejects_unsafe_configuration(self):
        os.environ["PROMETHEUS_BASE_URL"] = "http://169.254.169.254/latest"
        try:
            with patch("backend.tools.external_data_source.get_app_setting", return_value=None):
                unsafe = data_source_configuration_status()
                self.assertIn("prometheus", unsafe["unsafe_sources"])
                self.assertNotIn("prometheus", unsafe["configured_sources"])

                os.environ["PROMETHEUS_BASE_URL"] = "https://prom.example.com"
                safe = data_source_configuration_status()
                self.assertIn("prometheus", safe["configured_sources"])
                self.assertTrue(safe["has_real_data_source"])
        finally:
            os.environ.pop("PROMETHEUS_BASE_URL", None)

    def test_http_exception_returns_request_id_and_error_shape(self):
        with TestClient(app) as client:
            resp = client.get("/services/not-found-service")

        self.assertEqual(resp.status_code, 404)
        payload = resp.json()
        self.assertEqual(payload["error"], "request_failed")
        self.assertIn("request_id", payload)
        self.assertIn("detail", payload)

    def test_chat_confirm_guard(self):
        req_data = chat(ChatRequest(message="回滚 payment-service")).model_dump()
        self.assertTrue(req_data.get("requires_confirmation"))
        self.assertIn("policy_decision", req_data)

        os.environ["EXECUTION_GUARD_ENABLED"] = "true"
        os.environ["EXECUTION_GUARD_TOKEN"] = "guard-123"

        with self.assertRaises(HTTPException):
            confirm_action(ConfirmActionRequest(pending_action=req_data["pending_action"]), x_guard_token=None)

        allowed = confirm_action(
            ConfirmActionRequest(pending_action=req_data["pending_action"]),
            x_guard_token="guard-123",
        ).model_dump()
        self.assertEqual(allowed["intent"], "rollback")
        self.assertEqual(allowed["execution_mode"], "execute")

        dry_run_req = chat(ChatRequest(message="回滚 payment-service")).model_dump()
        dry_run = confirm_action(
            ConfirmActionRequest(pending_action=dry_run_req["pending_action"], dry_run=True),
            x_guard_token="guard-123",
        ).model_dump()
        self.assertEqual(dry_run["execution_mode"], "dry_run")
        self.assertIn("policy_decision", dry_run)

    def test_chat_deploy_is_server_side_confirmed_and_replay_safe(self):
        before_payment = get_service_status("payment-service")
        before_order = get_service_status("order-service")

        requested = chat(ChatRequest(message="部署 payment-service 到 v8.8.8")).model_dump()
        self.assertTrue(requested["requires_confirmation"])
        self.assertEqual(requested["execution_mode"], "pending_confirmation")
        self.assertEqual(get_service_status("payment-service")["version"], before_payment["version"])

        pending = dict(requested["pending_action"])
        change_request_id = pending["change_request_id"]
        pending["service_name"] = "order-service"
        pending["target_version"] = "v0.0.1-tampered"

        confirmed = confirm_action(ConfirmActionRequest(pending_action=pending)).model_dump()
        self.assertEqual(confirmed["intent"], "deploy")
        self.assertEqual(confirmed["execution_mode"], "execute")
        self.assertEqual(get_service_status("payment-service")["version"], "v8.8.8")
        self.assertEqual(get_service_status("order-service")["version"], before_order["version"])
        self.assertEqual(get_change_request(change_request_id)["status"], "executed")

        with self.assertRaises(HTTPException) as replay_error:
            confirm_action(ConfirmActionRequest(change_request_id=change_request_id))
        self.assertEqual(replay_error.exception.status_code, 409)

    def test_timeline_and_postmortem_available(self):
        chat(ChatRequest(message="payment-service 报警了，帮我排查"))

        timeline_data = timeline(limit=5).get("timeline", [])
        self.assertTrue(len(timeline_data) > 0)

        task_run_id = timeline_data[0]["id"]
        pm = postmortem(task_run_id=task_run_id).get("postmortem", {})
        for key in [
            "summary",
            "service_name",
            "incident_type",
            "impact",
            "symptoms",
            "likely_root_cause",
            "actions_taken",
            "current_status",
            "follow_ups",
            "narrative_summary",
        ]:
            self.assertIn(key, pm)

    def test_benchmark_and_replay_available(self):
        scenarios = benchmark_scenarios().get("scenarios", [])
        self.assertTrue(len(scenarios) >= 6)

        replay = benchmark_replay("payment_troubleshoot")
        self.assertEqual(replay["scenario"]["id"], "payment_troubleshoot")
        self.assertIn("evaluation", replay)
        self.assertIn("assessment_details", replay["result"])

        benchmark = benchmark_run()
        self.assertIn("summary", benchmark)
        self.assertIn("replays", benchmark)
        self.assertGreaterEqual(benchmark["summary"]["scenario_count"], 6)
        self.assertIn("average_score_rate", benchmark["summary"])
        self.assertIn("intent_accuracy", benchmark["summary"])
        self.assertIn("clarification_accuracy", benchmark["summary"])
        self.assertIn("evidence_hit_rate", benchmark["summary"])

    def test_benchmark_replay_supports_clarification_scenario(self):
        replay = benchmark_replay("payment_deploy_clarification")
        self.assertEqual(replay["scenario"]["id"], "payment_deploy_clarification")
        self.assertTrue(replay["result"]["requires_clarification"])
        self.assertIn("目标版本", replay["result"]["clarification_question"])
        self.assertTrue(
            any(
                check["name"] == "clarification_question_keywords" and check["passed"]
                for check in replay["evaluation"]["checks"]
            )
        )

    def test_llm_error_fallback_observable(self):
        os.environ["DEEPSEEK_API_KEY"] = "dummy"

        with patch("backend.llm.provider.request.urlopen", side_effect=error.URLError("boom")):
            data = chat(ChatRequest(message="payment-service 状态")).model_dump()

        self.assertEqual(data["generation_source"], "fallback_llm_error")
        self.assertEqual(data["fallback_reason"], "request_error")
        self.assertTrue(data["used_fallback"])

    def test_llm_token_and_cost_capture(self):
        os.environ["DEEPSEEK_API_KEY"] = "dummy"
        os.environ["SRE_LLM_INPUT_COST_PER_MILLION_USD"] = "0.14"
        os.environ["SRE_LLM_OUTPUT_COST_PER_MILLION_USD"] = "0.28"
        capture_token = begin_llm_usage_capture()
        try:
            with patch(
                "backend.llm.provider.request.urlopen",
                return_value=MockHttpResponse(
                    {
                        "choices": [{"message": {"content": "healthy"}}],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 50,
                            "total_tokens": 150,
                        },
                    }
                ),
            ):
                result = _request_chat_completion(
                    [{"role": "user", "content": "status"}]
                )
            self.assertTrue(result["ok"])
            captured = finish_llm_usage_capture(capture_token)
            capture_token = None
            self.assertEqual(captured["call_count"], 1)
            self.assertEqual(captured["input_tokens"], 100)
            self.assertEqual(captured["output_tokens"], 50)
            self.assertEqual(captured["total_tokens"], 150)
            self.assertEqual(captured["cost_usd_micros"], 28)
            self.assertEqual(captured["providers"], ["deepseek"])
        finally:
            if capture_token is not None:
                finish_llm_usage_capture(capture_token)
            os.environ.pop("DEEPSEEK_API_KEY", None)
            os.environ.pop("SRE_LLM_INPUT_COST_PER_MILLION_USD", None)
            os.environ.pop("SRE_LLM_OUTPUT_COST_PER_MILLION_USD", None)

    def test_deploy_guard_denied_when_enabled(self):
        os.environ["EXECUTION_GUARD_ENABLED"] = "true"
        os.environ["EXECUTION_GUARD_TOKEN"] = "guard-123"

        pending = deploy(DeployRequest(service_name="payment-service", new_version="v9.9.9"))
        self.assertEqual(pending["execution_mode"], "pending_confirmation")

        with self.assertRaises(HTTPException) as denied:
            confirm_change(
                pending["change_request_id"],
                ChangeConfirmRequest(),
                x_guard_token="wrong",
            )
        self.assertEqual(denied.exception.status_code, 403)

        allowed = confirm_change(
            pending["change_request_id"],
            ChangeConfirmRequest(dry_run=True),
            x_guard_token="guard-123",
        )
        self.assertEqual(allowed["execution_mode"], "dry_run")
        self.assertEqual(get_change_request(pending["change_request_id"])["status"], "dry_run")

    def test_direct_api_uses_change_request_and_blocks_replay(self):
        before = get_service_status("payment-service")["version"]
        pending = deploy(DeployRequest(service_name="payment-service", new_version="v6.6.6"))

        self.assertTrue(pending["requires_confirmation"])
        self.assertEqual(get_service_status("payment-service")["version"], before)

        result = confirm_change(
            pending["change_request_id"],
            ChangeConfirmRequest(),
            x_guard_token=None,
        )
        self.assertEqual(result["execution_mode"], "execute")
        self.assertEqual(get_service_status("payment-service")["version"], "v6.6.6")

        with self.assertRaises(HTTPException) as replay:
            confirm_change(
                pending["change_request_id"],
                ChangeConfirmRequest(),
                x_guard_token=None,
            )
        self.assertEqual(replay.exception.status_code, 409)

    def test_webhook_executor_is_idempotent_and_requires_verification(self):
        os.environ["SRE_CHANGE_EXECUTOR"] = "webhook"
        os.environ["SRE_CHANGE_EXECUTOR_WEBHOOK_URL"] = "https://executor.example.com/changes"
        os.environ["SRE_CHANGE_EXECUTOR_TOKEN"] = "executor-token"
        captured = {}

        def verified_executor(req, timeout=30):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["idempotency_key"] = req.get_header("Idempotency-key")
            captured["authorization"] = req.get_header("Authorization")
            captured["timeout"] = timeout
            return MockHttpResponse(
                {
                    "success": True,
                    "verified": True,
                    "execution_reference": "argo:sync:123",
                    "message": "rollout healthy",
                    "observed_state": {
                        "version": "v-webhook",
                        "status": "running",
                        "error_rate": 0.1,
                    },
                }
            )

        try:
            pending = deploy(
                DeployRequest(
                    service_name="payment-service",
                    new_version="v-webhook",
                )
            )
            with patch(
                "backend.executors.change_executor.request.urlopen",
                side_effect=verified_executor,
            ):
                result = confirm_change(
                    pending["change_request_id"],
                    ChangeConfirmRequest(),
                    x_guard_token=None,
                )

            self.assertEqual(result["execution_mode"], "execute")
            self.assertEqual(captured["body"]["change_request_id"], pending["change_request_id"])
            self.assertEqual(captured["idempotency_key"], pending["change_request_id"])
            self.assertEqual(captured["authorization"], "Bearer executor-token")

            unverified = deploy(
                DeployRequest(
                    service_name="payment-service",
                    new_version="v-unverified",
                )
            )
            with patch(
                "backend.executors.change_executor.request.urlopen",
                return_value=MockHttpResponse(
                    {"success": True, "verified": False, "message": "accepted only"}
                ),
            ):
                rejected = confirm_change(
                    unverified["change_request_id"],
                    ChangeConfirmRequest(),
                    x_guard_token=None,
                )
            self.assertEqual(rejected["execution_mode"], "failed")
            self.assertEqual(get_change_request(unverified["change_request_id"])["status"], "failed")
        finally:
            os.environ.pop("SRE_CHANGE_EXECUTOR", None)
            os.environ.pop("SRE_CHANGE_EXECUTOR_WEBHOOK_URL", None)
            os.environ.pop("SRE_CHANGE_EXECUTOR_TOKEN", None)

    def test_durable_change_queue_worker_and_cancellation(self):
        os.environ["SRE_CHANGE_EXECUTION_MODE"] = "queued"
        os.environ["SRE_CHANGE_JOB_MAX_ATTEMPTS"] = "1"
        before = get_service_status("payment-service")["version"]

        pending = deploy(
            DeployRequest(
                service_name="payment-service",
                new_version="v-queued-worker",
            )
        )
        queued = confirm_change(
            pending["change_request_id"],
            ChangeConfirmRequest(),
            x_guard_token=None,
        )
        self.assertEqual(queued["execution_mode"], "queued")
        self.assertEqual(get_service_status("payment-service")["version"], before)
        self.assertEqual(get_change_request(pending["change_request_id"])["status"], "queued")
        self.assertEqual(get_change_job(pending["change_request_id"])["status"], "queued")

        processed = None
        for _ in range(20):
            candidate = process_next_change_job("regression-worker")
            if candidate and candidate["change_request_id"] == pending["change_request_id"]:
                processed = candidate
                break
        self.assertIsNotNone(processed)
        self.assertEqual(processed["status"], "executed")
        self.assertEqual(get_service_status("payment-service")["version"], "v-queued-worker")
        self.assertEqual(get_change_job(pending["change_request_id"])["status"], "succeeded")

        cancellable = deploy(
            DeployRequest(
                service_name="payment-service",
                new_version="v-cancelled-worker",
            )
        )
        confirm_change(
            cancellable["change_request_id"],
            ChangeConfirmRequest(),
            x_guard_token=None,
        )
        cancelled = cancel_change(
            cancellable["change_request_id"],
            ChangeCancelRequest(reason="maintenance window closed"),
        )
        self.assertEqual(cancelled["change_request"]["status"], "cancelled")
        self.assertEqual(get_change_job(cancellable["change_request_id"])["status"], "cancelled")

        with self.assertRaises(HTTPException) as replay:
            confirm_change(
                cancellable["change_request_id"],
                ChangeConfirmRequest(),
                x_guard_token=None,
            )
        self.assertEqual(replay.exception.status_code, 409)

    def test_failed_change_job_requires_guarded_admin_redrive(self):
        os.environ["SRE_CHANGE_EXECUTION_MODE"] = "queued"
        os.environ["SRE_CHANGE_JOB_MAX_ATTEMPTS"] = "1"
        os.environ["SRE_CHANGE_JOB_MAX_REDRIVES"] = "2"
        pending = deploy(
            DeployRequest(
                service_name="payment-service",
                new_version="v-redrive-worker",
            )
        )
        confirm_change(
            pending["change_request_id"],
            ChangeConfirmRequest(),
            x_guard_token=None,
        )
        with patch(
            "backend.services.change_worker_service.execute_confirmed_action",
            side_effect=RuntimeError("executor transport failed"),
        ):
            failed = process_next_change_job("failing-regression-worker")
        self.assertEqual(failed["status"], "unknown")
        self.assertEqual(get_change_job(pending["change_request_id"])["status"], "unknown")
        self.assertEqual(get_change_request(pending["change_request_id"])["status"], "unknown")

        os.environ["EXECUTION_GUARD_ENABLED"] = "true"
        os.environ["EXECUTION_GUARD_TOKEN"] = "redrive-guard-token"
        try:
            with TestClient(app) as client:
                denied = client.post(
                    f"/changes/{pending['change_request_id']}/redrive",
                    json={"reason": "transient executor outage"},
                )
                self.assertEqual(denied.status_code, 403)
                redriven = client.post(
                    f"/changes/{pending['change_request_id']}/redrive",
                    headers={"X-Guard-Token": "redrive-guard-token"},
                    json={"reason": "transient executor outage"},
                )
                self.assertEqual(redriven.status_code, 200)
                self.assertEqual(redriven.json()["job"]["status"], "queued")
                self.assertEqual(redriven.json()["job"]["redrive_count"], 1)
        finally:
            os.environ["EXECUTION_GUARD_ENABLED"] = "false"
            os.environ.pop("EXECUTION_GUARD_TOKEN", None)

        processed = process_next_change_job("redrive-regression-worker")
        self.assertEqual(processed["status"], "executed")
        final_job = get_change_job(pending["change_request_id"])
        self.assertEqual(final_job["status"], "succeeded")
        self.assertEqual(final_job["redrive_count"], 1)
        self.assertEqual(get_service_status("payment-service")["version"], "v-redrive-worker")

    def test_direct_api_supports_dry_run(self):
        deploy_preview = deploy(DeployRequest(service_name="payment-service", new_version="v1.2.3", dry_run=True))
        self.assertEqual(deploy_preview["mode"], "dry_run")
        self.assertIn("policy_decision", deploy_preview)

        rollback_preview = rollback(RollbackRequest(service_name="payment-service", dry_run=True))
        self.assertEqual(rollback_preview["mode"], "dry_run")
        self.assertIn("policy_decision", rollback_preview)

    def test_deploy_policy_uses_k8s_runtime_health(self):
        mocked_k8s = {
            "rollout": {"rollout_status": "degraded"},
            "summary": {"unhealthy_pods": 2, "restarting_pods": 1},
            "events": [{"type": "Warning", "reason": "BackOff"}],
        }

        with patch("backend.services.policy_service.get_external_k8s_observability", return_value=mocked_k8s):
            preview = deploy(DeployRequest(service_name="payment-service", new_version="v9.9.9", dry_run=True))

        policy = preview["policy_decision"]
        self.assertEqual(policy["risk_level"], "high")
        self.assertEqual(policy["recommended_mode"], "dry_run")
        self.assertIn("k8s_runtime_unhealthy", policy["reasons"])
        self.assertIn("k8s_rollout_unstable", policy["reasons"])
        self.assertIn("检查 Deployment rollout、Pod 就绪状态和近期 Warning 事件", preview["preview_steps"])

    def test_prometheus_and_loki_datasource_path(self):
        self._stop_external_source_patches()

        def fake_get_app_setting(key):
            values = {
                "SRE_DATA_API_BASE": None,
                "SRE_DATA_API_TOKEN": None,
                "PROMETHEUS_BASE_URL": "http://prom.example.com",
                "PROMETHEUS_TOKEN": None,
                "PROMETHEUS_SERVICE_LABEL": "service",
                "LOKI_BASE_URL": "http://loki.example.com",
                "LOKI_TOKEN": None,
                "LOKI_SERVICE_LABEL": "service",
            }
            return values.get(key)

        def fake_urlopen(req, timeout=5):
            parsed = urlparse(req.full_url)
            query = parse_qs(parsed.query)

            if parsed.netloc == "prom.example.com" and parsed.path == "/api/v1/label/service/values":
                return MockHttpResponse({"status": "success", "data": ["payment-service"]})

            if parsed.netloc == "prom.example.com" and parsed.path == "/api/v1/query":
                promql = query.get("query", [""])[0]
                if promql.startswith("sum(up"):
                    return MockHttpResponse({"status": "success", "data": {"result": [{"value": [0, "1"]}]}})
                if promql.startswith("count(up"):
                    return MockHttpResponse({"status": "success", "data": {"result": [{"value": [0, "3"]}]}})
                if "http_requests_total" in promql and "5.." in promql:
                    return MockHttpResponse({"status": "success", "data": {"result": [{"value": [0, "7.5"]}]}})
                if "process_cpu_seconds_total" in promql:
                    return MockHttpResponse({"status": "success", "data": {"result": [{"value": [0, "42"]}]}})
                if "process_resident_memory_bytes" in promql:
                    return MockHttpResponse({"status": "success", "data": {"result": [{"value": [0, "256"]}]}})
                if "histogram_quantile" in promql:
                    return MockHttpResponse({"status": "success", "data": {"result": [{"value": [0, "123"]}]}})

            if parsed.netloc == "loki.example.com" and parsed.path == "/loki/api/v1/query_range":
                return MockHttpResponse({
                    "status": "success",
                    "data": {
                        "result": [
                            {
                                "stream": {"service": "payment-service"},
                                "values": [["1710000000000000000", "ERROR database connection timeout from loki"]],
                            }
                        ]
                    },
                })

            raise error.URLError("unexpected_url")

        with patch("backend.tools.external_data_source.get_app_setting", side_effect=fake_get_app_setting):
            with patch("backend.tools.external_data_source.request.urlopen", side_effect=fake_urlopen):
                services = get_external_services()
                metrics = get_external_metrics("payment-service")
                logs = get_external_logs("payment-service", limit=5)

        self.assertTrue(any(item["name"] == "payment-service" for item in services))
        self.assertEqual(metrics["status"], "degraded")
        self.assertEqual(metrics["replicas"], 3)
        self.assertEqual(logs[0]["level"], "ERROR")

    def test_custom_prometheus_query_template_is_used(self):
        self._stop_external_source_patches()

        observed_queries = []

        def fake_get_app_setting(key):
            values = {
                "SRE_DATA_API_BASE": None,
                "SRE_DATA_API_TOKEN": None,
                "PROMETHEUS_BASE_URL": "http://prom.example.com",
                "PROMETHEUS_TOKEN": None,
                "PROMETHEUS_SERVICE_LABEL": "app_name",
                "PROM_QUERY_UP": "sum(custom_up_metric{service_selector})",
                "PROM_QUERY_REPLICAS": "count(custom_up_metric{service_selector})",
                "PROM_QUERY_ERROR_RATE": "0",
                "PROM_QUERY_CPU": "11",
                "PROM_QUERY_MEMORY": "22",
                "PROM_QUERY_LATENCY_P95_MS": "33",
                "LOKI_BASE_URL": None,
            }
            return values.get(key)

        def fake_urlopen(req, timeout=5):
            parsed = urlparse(req.full_url)
            query = parse_qs(parsed.query)
            if parsed.netloc == "prom.example.com" and parsed.path == "/api/v1/label/app_name/values":
                return MockHttpResponse({"status": "success", "data": ["checkout-api"]})
            if parsed.netloc == "prom.example.com" and parsed.path == "/api/v1/query":
                promql = query.get("query", [""])[0]
                observed_queries.append(promql)
                return MockHttpResponse({"status": "success", "data": {"result": [{"value": [0, "1"]}]}})
            raise error.URLError("unexpected_url")

        with patch("backend.tools.external_data_source.get_app_setting", side_effect=fake_get_app_setting):
            with patch("backend.tools.external_data_source.request.urlopen", side_effect=fake_urlopen):
                metrics = get_external_metrics("checkout-api")

        self.assertEqual(metrics["service"], "checkout-api")
        self.assertTrue(any("custom_up_metric" in query for query in observed_queries))

    def test_k8s_observability_path_returns_rollout_pods_and_events(self):
        self._stop_external_source_patches()

        def fake_get_app_setting(key):
            values = {
                "SRE_DATA_API_BASE": None,
                "K8S_API_BASE": "http://k8s.example.com",
                "K8S_API_TOKEN": None,
                "K8S_NAMESPACE": "payments",
                "K8S_SERVICE_LABEL": "app",
            }
            return values.get(key)

        def fake_urlopen(req, timeout=5):
            parsed = urlparse(req.full_url)
            query = parse_qs(parsed.query)
            if parsed.netloc == "k8s.example.com" and parsed.path == "/apis/apps/v1/namespaces/payments/deployments/payment-service":
                return MockHttpResponse({
                    "metadata": {"name": "payment-service"},
                    "spec": {"replicas": 3},
                    "status": {
                        "readyReplicas": 2,
                        "availableReplicas": 2,
                        "updatedReplicas": 3,
                        "unavailableReplicas": 1,
                        "conditions": [{"type": "Progressing", "status": "True", "message": "ReplicaSet updated"}],
                    },
                })
            if parsed.netloc == "k8s.example.com" and parsed.path == "/api/v1/namespaces/payments/pods":
                self.assertEqual(query.get("labelSelector", [""])[0], "app=payment-service")
                return MockHttpResponse({
                    "items": [
                        {
                            "metadata": {"name": "payment-service-abc"},
                            "status": {
                                "phase": "Running",
                                "nodeName": "node-a",
                                "startTime": "2026-04-13T10:00:00Z",
                                "containerStatuses": [{"ready": True, "restartCount": 0}],
                            },
                        },
                        {
                            "metadata": {"name": "payment-service-def"},
                            "status": {
                                "phase": "Running",
                                "nodeName": "node-b",
                                "startTime": "2026-04-13T10:01:00Z",
                                "containerStatuses": [{"ready": False, "restartCount": 4}],
                            },
                        },
                    ]
                })
            if parsed.netloc == "k8s.example.com" and parsed.path == "/api/v1/namespaces/payments/events":
                return MockHttpResponse({
                    "items": [
                        {
                            "type": "Warning",
                            "reason": "BackOff",
                            "message": "Back-off restarting failed container",
                            "count": 3,
                            "lastTimestamp": "2026-04-13T10:05:00Z",
                            "involvedObject": {"kind": "Pod", "name": "payment-service-def"},
                        }
                    ]
                })
            raise error.URLError("unexpected_url")

        with patch("backend.tools.external_data_source.get_app_setting", side_effect=fake_get_app_setting):
            with patch("backend.tools.external_data_source.request.urlopen", side_effect=fake_urlopen):
                data = get_external_k8s_observability("payment-service", namespace="payments")

        self.assertEqual(data["rollout"]["rollout_status"], "degraded")
        self.assertEqual(data["summary"]["pod_count"], 2)
        self.assertEqual(data["summary"]["restarting_pods"], 1)
        self.assertEqual(data["events"][0]["reason"], "BackOff")

    def test_llm_intent_router_understands_natural_language(self):
        with patch("backend.agents.intent_router.classify_intent_with_llm", return_value="troubleshoot"):
            data = chat(ChatRequest(message="帮我看看 payment-service 最近是不是有问题")).model_dump()

        self.assertEqual(data["intent"], "troubleshoot")

    def test_llm_entity_extraction_can_fill_service_name(self):
        with patch(
            "backend.agents.intent_router.extract_entities_with_llm",
            return_value={
                "intent": "status_query",
                "service_name": "payment-service",
                "env": "prod",
                "version": None,
            },
        ):
            data = chat(ChatRequest(message="帮我看看支付那个服务现在怎么样")).model_dump()

        self.assertEqual(data["intent"], "status_query")
        self.assertIn("payment-service", data["final_answer"])

    def test_chat_session_context_supports_follow_up_action(self):
        session_id = "regression-session-1"

        first = chat(ChatRequest(message="payment-service 状态", session_id=session_id)).model_dump()
        self.assertEqual(first["intent"], "status_query")
        self.assertEqual(first["session_id"], session_id)

        second = chat(ChatRequest(message="那就回滚吧", session_id=session_id)).model_dump()
        self.assertEqual(second["intent"], "rollback")
        self.assertEqual(second["session_id"], session_id)
        self.assertTrue(second["requires_confirmation"])
        self.assertEqual(second["pending_action"]["service_name"], "payment-service")

    def test_clarification_flow_can_resume_deploy(self):
        session_id = f"regression-clarification-deploy-{os.getpid()}-{id(self)}"

        first = chat(ChatRequest(message="部署 payment-service", session_id=session_id)).model_dump()
        self.assertEqual(first["intent"], "deploy")
        self.assertTrue(first["requires_clarification"])
        self.assertIn("目标版本", first["clarification_question"])

        second = chat(ChatRequest(message="v1.2.3", session_id=session_id)).model_dump()
        self.assertEqual(second["intent"], "deploy")
        self.assertFalse(second["requires_clarification"])
        self.assertIn("payment-service", second["final_answer"])
        self.assertIn("v1.2.3", second["final_answer"])

        session = get_chat_session_context(session_id)
        self.assertIsNone(session["pending_intent"])
        self.assertIsNone(session["pending_question"])

    def test_clarification_flow_can_select_service_by_option_index(self):
        session_id = f"regression-clarification-option-{os.getpid()}-{id(self)}"

        first = chat(ChatRequest(message="回滚", session_id=session_id)).model_dump()
        self.assertEqual(first["intent"], "rollback")
        self.assertTrue(first["requires_clarification"])
        self.assertTrue(len(first["clarification_options"] or []) >= 1)

        second = chat(ChatRequest(message="1", session_id=session_id)).model_dump()
        self.assertEqual(second["intent"], "rollback")
        self.assertFalse(second["requires_clarification"])
        self.assertTrue(second["requires_confirmation"])
        self.assertEqual(
            second["pending_action"]["service_name"],
            first["clarification_options"][0],
        )

    def test_extended_entity_extraction_supports_ops_context(self):
        with patch(
            "backend.agents.intent_router.extract_entities_with_llm",
            return_value={
                "intent": "troubleshoot",
                "service_name": "payment-service",
                "action_target": "payment-service",
                "env": "prod",
                "namespace": "payments",
                "cluster": "prod-sh",
                "region": "cn-sh1",
                "version": None,
                "time_window_minutes": 30,
            },
        ):
            entities = extract_entities("帮我看下 prod 集群 prod-sh 里 payment-service 最近30分钟在 payments namespace 有没有异常")

        self.assertEqual(entities["intent"], "troubleshoot")
        self.assertEqual(entities["service_name"], "payment-service")
        self.assertEqual(entities["action_target"], "payment-service")
        self.assertEqual(entities["env"], "prod")
        self.assertEqual(entities["namespace"], "payments")
        self.assertEqual(entities["cluster"], "prod-sh")
        self.assertEqual(entities["region"], "cn-sh1")
        self.assertEqual(entities["time_window_minutes"], 30)

    def test_chat_session_context_persists_extended_entities(self):
        session_id = "regression-session-entities"

        with patch(
            "backend.agents.intent_router.extract_entities_with_llm",
            return_value={
                "intent": "troubleshoot",
                "service_name": "payment-service",
                "action_target": "payment-service",
                "env": "prod",
                "namespace": "payments",
                "cluster": "prod-sh",
                "region": "cn-sh1",
                "version": None,
                "time_window_minutes": 30,
            },
        ):
            chat(ChatRequest(message="帮我看下 payment-service 最近30分钟", session_id=session_id))

        session = get_chat_session_context(session_id)
        self.assertEqual(session["last_service_name"], "payment-service")
        self.assertEqual(session["last_action_target"], "payment-service")
        self.assertEqual(session["last_env"], "prod")
        self.assertEqual(session["last_namespace"], "payments")
        self.assertEqual(session["last_cluster"], "prod-sh")
        self.assertEqual(session["last_region"], "cn-sh1")
        self.assertEqual(session["last_time_window_minutes"], 30)


if __name__ == "__main__":
    unittest.main()
