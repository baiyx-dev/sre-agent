# SRE Agent 免费试用与后续付费试点交付清单

本清单用于把当前版本交付给第一个设计合作客户。当前商业安全边界是“每客户独立部署、单工作区”，不允许多个互不信任客户共享同一实例或数据库。

## 1. 试点范围

- 先接入 5–20 个非核心或可快速回退的 Kubernetes 服务。
- 第一阶段只开放查询、诊断、Incident 归并和 dry-run。
- 第二阶段只在测试环境开放真实变更，执行器必须返回 `success: true` 和 `verified: true`。
- 明确试点负责人、客户技术负责人、升级联系人、变更窗口和退出条件。

## 2. 上线前硬门槛

- PostgreSQL 独立实例可用，已启用 PITR/快照；`/health/ready` 显示 `database_backend=postgresql`。
- Web 与 Worker 使用同一 `DATABASE_URL`，且 `SRE_CHANGE_EXECUTION_MODE=queued`。
- 认证、执行保护均开启；环境 Bootstrap Key 和 Guard Token 来自密钥管理器。
- 生产关闭数据库密钥存储，外部地址采用精确 allowlist，禁止通配内网访问。
- 已接入一个真实 Prometheus、Loki 和 Kubernetes 集群，并验证查询权限只读。
- 自助试用实例已配置至少 32 位的恢复管理员 Key 和有效升级联系地址，`/health/ready` 的 `trial_recovery_admin`、`trial_upgrade_contact` 均为 true。
- 生产未灌入演示服务；`/health/ready` 的 `real_data_source` 为 true，并已实际查询到带时间戳的客户遥测数据。
- 若开放写操作，Webhook executor 已在测试环境完成幂等、超时、失败和发布后健康验证。
- 依赖审计、全部回归测试、容器非 root 检查和 PostgreSQL CI 均通过。
- 全新可销毁实例已运行一次 `scripts/trial_activation_smoke.py --confirm-disposable-instance`；已领取实例的 `scripts/smoke_test.py` 与目标容量参数下的 `scripts/load_smoke.py` 均通过，结果归档并形成客户 SLO 基线。
- 本地交付使用 `compose.yaml + compose.trial.yaml`，CI 已真实启动 PostgreSQL、Web、Worker 并通过完整领取验收；客户卷未使用 `down --volumes` 删除。
- 完成一次备份恢复演练，记录实际 RTO/RPO。
- `/audit/verify` 返回 `valid=true`，当前链头已写入客户侧不可变归档并完成一次恢复后复验。

任一硬门槛不满足时，只能作为本机演示，不能声明生产可用。

## 3. 客户实例配置

```env
SRE_ENVIRONMENT=production
DATABASE_URL=postgresql://...
SRE_AUTH_ENABLED=true
SRE_ADMIN_API_KEY=<bootstrap-secret>
EXECUTION_GUARD_ENABLED=true
EXECUTION_GUARD_TOKEN=<guard-secret>
SRE_ALLOW_INSECURE_DB_SECRETS=false
SRE_ALLOW_PRODUCTION_SQLITE=false
SRE_SEED_DEMO_DATA=false
SRE_REQUIRE_REAL_DATA_SOURCE=true

SRE_WORKSPACE_ID=<customer-slug>
SRE_WORKSPACE_NAME=<customer-name>
SRE_PLAN=team
SRE_MONTHLY_REQUEST_LIMIT=100000
SRE_SUBSCRIPTION_STATUS=active
SRE_TRIAL_DAYS=14
SRE_TRIAL_ENDS_AT=
SRE_CURRENT_PERIOD_END=

SRE_CHANGE_EXECUTION_MODE=queued
SRE_CHANGE_EXECUTOR=simulation
SRE_PRODUCTION_WRITE_ENABLED=false
```

首次启动后使用 Bootstrap Admin Key 创建至少两把工作区 admin 密钥，验证 `/auth/me` 后轮换 Bootstrap Key。所有日常调用使用工作区密钥；明文密钥只在创建响应出现一次。

## 4. 验收脚本

1. `/health/live` 返回 200，`/health/ready` 所有检查为 true。
2. viewer 可查询服务、指标、日志、Incident 和用量，但不能部署或修改配置。
3. operator 可创建变更请求，缺少 Guard Token 时无法确认真实写操作。
4. 同一变更请求并发确认只有一次成功；过期、取消和重放均返回确定状态。
5. Worker 重启后可恢复租约任务；不确定结果进入 `unknown`，核对执行器后可用原幂等键受控重驱，不出现重复执行。
6. 撤销的 API Key 立即返回 401；最后一把工作区 admin Key 无法被撤销。
7. `/billing/usage` 的 `api_request`、`chat_request`、`change_confirmation` 与实际调用一致。
8. Prometheus 可采集请求延迟、错误率、Incident、变更队列和月度用量指标。
9. 恢复到隔离数据库后能查询最近 Incident、变更请求和审计记录。
10. `/audit/verify` 在正常、保留清理 checkpoint 和恢复场景均为 true；隔离篡改一条审计后必须变为 false。
11. 重放同一个 `/billing/pilot-outcomes` 幂等键不会重复计数，修改载荷重放返回 409。
12. `/billing/value-report` 与 CSV 中的节省时间、支持工时、模型成本和毛利可从原始证据复算。
13. `/billing/subscription` 显示合同对应状态；试用到期后业务 API 返回 402，而账单、价值报告和监控仍可导出。
14. 已结束月份能够预览并幂等定稿；迟到用量不改变已定稿账单，`/verify` 返回 `valid=true`，CSV 与 JSON 金额一致。
15. 新免费试用实例在领取前为 `pending_activation` 且不消耗试用天数；正确令牌只可领取一次，返回的首把 admin Key 可通过 `/auth/me` 验证。
16. `/trial/onboarding` 的接入、首次查询、首次诊断和反馈里程碑能从数据库事实复算；不可达或尚未验证的目标不能完成接入，无证据查询或排障不能完成首次查询/首次价值，反馈幂等重放不重复计数，修改载荷返回 409。

## 5. 价值与单位经济性

试点开始前记录最近 4 周基线，试点期间每周导出：

- 告警到首次有效结论的中位数与 P95 时间。
- Incident MTTA/MTTR、诊断成功数和建议采纳率。
- 每次诊断的人工时长、模型调用数和推理成本。
- 变更请求数、确认率、成功率、失败率和人工回退次数。
- 周活跃 SRE、接入服务数、用量额度消耗和支持工时。

每次完成诊断、Incident 或受控变更后，由管理员写入 `/billing/pilot-outcomes`；每周使用 `/billing/value-report.csv?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` 固化证据。上线前配置合同月费、月基础设施成本、客户全成本时薪和支持时薪；字段定义与示例见 [PILOT_VALUE_REPORTING.md](PILOT_VALUE_REPORTING.md)。报告使用 UTC，日期范围首尾均包含，最长 366 天。

付费验证门槛建议为：连续 4 周节省的人力成本明显高于托管、推理、实施和支持总成本，并由客户负责人确认愿意续费或扩容。

## 6. 当前不能对客户承诺

- 共享数据库的行级多租户隔离。
- OIDC/SSO、SCIM、支付网关、自动开票和正式 SLA。
- 未在客户目标 PostgreSQL 上验证过的容量、高可用和灾难恢复指标。
- 未接入真实发布平台时的自动生产变更。
- 未完成外部不可变审计归档时的合规认证。

这些能力必须在后续里程碑完成并经过真实环境验证后，才能进入标准商业合同。
