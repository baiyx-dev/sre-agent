import os
import unittest
from unittest.mock import patch
from urllib import error
from urllib.parse import parse_qs, urlparse
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api.routes_chat import chat, confirm_action
from backend.api.routes_incidents import postmortem, timeline, deploy, rollback, benchmark_run, benchmark_replay, benchmark_scenarios
from backend.schemas.chat import ChatRequest, ConfirmActionRequest
from backend.api.routes_incidents import DeployRequest, RollbackRequest
from backend.agents.intent_router import extract_entities
from backend.main import app
from backend.storage.db import init_db
from backend.storage.repositories import get_chat_session_context
from backend.storage.seed import seed_data
from backend.tools.external_data_source import (
    get_external_k8s_observability,
    get_external_logs,
    get_external_metrics,
    get_external_services,
)


class MockHttpResponse:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self.payload

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
        os.environ["EXECUTION_GUARD_ENABLED"] = "false"
        os.environ.pop("EXECUTION_GUARD_TOKEN", None)
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

    def test_internal_metrics_exposes_success_rate_and_response_time(self):
        with TestClient(app) as client:
            client.get("/services/")
            resp = client.get("/internal/metrics")

        self.assertEqual(resp.status_code, 200)
        metrics = resp.json()["metrics"]
        self.assertGreaterEqual(metrics["request_count"], 2)
        self.assertIn("success_rate_pct", metrics)
        self.assertIn("avg_response_time_ms", metrics)
        self.assertIn("p95_response_time_ms", metrics)

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

        dry_run = confirm_action(
            ConfirmActionRequest(pending_action=req_data["pending_action"], dry_run=True),
            x_guard_token="guard-123",
        ).model_dump()
        self.assertEqual(dry_run["execution_mode"], "dry_run")
        self.assertIn("policy_decision", dry_run)

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

    def test_deploy_guard_denied_when_enabled(self):
        os.environ["EXECUTION_GUARD_ENABLED"] = "true"
        os.environ["EXECUTION_GUARD_TOKEN"] = "guard-123"

        with self.assertRaises(HTTPException):
            deploy(DeployRequest(service_name="payment-service", new_version="v9.9.9"), x_guard_token="wrong")

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
