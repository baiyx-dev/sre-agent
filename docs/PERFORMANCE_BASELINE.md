# SRE Agent 性能与启动基线

## 2026-07-19 本地开发基线

环境：Windows 本地进程、SQLite、Uvicorn 单实例、认证开启。此结果用于发现明显性能回归，不代表生产 PostgreSQL 容量承诺。

进程级烟测覆盖：

- liveness、readiness 与 schema 版本
- W3C trace context 与响应 Trace ID
- 未认证拒绝、admin 身份识别
- 服务查询、聊天诊断、dry-run、Prometheus 指标

并发结果：

| 指标 | 结果 |
|---|---:|
| 请求数 | 100 |
| 并发 | 10 |
| 成功率 | 100% |
| P50 | 137.31 ms |
| P95 | 358.12 ms |
| 最大延迟 | 380.32 ms |
| 吞吐 | 63.03 RPS |

同日加入持久化 Worker 心跳后，使用隔离 SQLite、认证 Web、`SRE_CHANGE_EXECUTION_MODE=queued` 和独立 Worker 进程复测：readiness 检测到 1 个活跃 Worker，进程烟测全部通过；100 请求、并发 10 的成功率为 100%，P50 135.23 ms、P95 261.38 ms、最大 297.31 ms、吞吐 65.64 RPS。

CI 门槛为 100 请求全部成功且 P95 不超过 1,000 ms。该门槛只做回归保护；客户上线前必须在目标 PostgreSQL、实际 Web/Worker 副本数和真实数据源下分别测试读请求、Incident 归并、队列积压与恢复，并按合同容量设定正式 SLO。

运行方式：

```bash
python scripts/smoke_test.py --base-url http://127.0.0.1:8000 --api-key <admin-key>
python scripts/load_smoke.py --base-url http://127.0.0.1:8000 --api-key <admin-key> --requests 100 --concurrency 10
```
