# SRE Agent 运维手册

## 健康检查

- `/health/live` 只表示进程存活。
- `/health/ready` 检查数据库、认证、执行保护、密钥模式、真实数据源配置、执行器与执行模式；生产配置不完整时返回 503。
- `/metrics` 需要 viewer 权限，包含 HTTP 指标和变更队列深度。
- 生产默认输出 JSON 结构化日志；请求响应带 `X-Trace-Id` 和 W3C `traceparent`。计量写失败由 `sre_agent_usage_metering_failures_total` 告警。

生产环境默认关闭演示 seed（`SRE_SEED_DEMO_DATA=false`）并要求真实数据源（`SRE_REQUIRE_REAL_DATA_SOURCE=true`）。`real_data_source` 只证明至少一个数据源或受监控目标已配置且 URL 通过 SSRF 校验，不替代连通性、最小只读权限和数据新鲜度验收。`SRE_REQUIRE_REAL_DATA_SOURCE=false` 只能用于隔离评估。

## SQLite 单节点备份

SQLite 仅作为本地演示和单节点试点存储，不支持多实例高可用。在线备份使用 SQLite Backup API，结束后执行完整性检查并写入 SHA-256 manifest：

```bash
python -m backend.maintenance backup --output-dir ./backups
python -m backend.maintenance verify --source ./backups/sre-agent-YYYYMMDDTHHMMSSZ.sqlite3
```

备份文件必须复制到独立故障域/对象存储，并根据客户数据保留策略设置生命周期。只在备份和 manifest 均存在且校验通过时计为成功。

## 恢复演练

恢复命令默认拒绝覆盖已有文件，应先恢复到新的路径并验证：

```bash
python -m backend.maintenance restore \
  --source ./backups/sre-agent-YYYYMMDDTHHMMSSZ.sqlite3 \
  --target ./restore-test/sre_agent.db
```

恢复验收：

1. 命令返回 `integrity: ok`，SHA-256 和文件大小非空。
2. 使用恢复库启动隔离实例，`/health/ready` 返回 200。
3. 核对变更请求、执行审计和最近事故记录。
4. 执行一次只读诊断和 dry-run，不进行真实写操作。
5. 记录实际 RTO/RPO；生产试点至少每月演练一次。

## 耐久变更 Worker

启用 `SRE_CHANGE_EXECUTION_MODE=queued` 后独立运行：

```bash
python -m backend.worker
```

Worker 使用原子领取和租约恢复。只有执行器支持幂等键时才能设置 `SRE_CHANGE_JOB_MAX_ATTEMPTS>1`。queued 模式下 `/health/ready` 要求至少一个新鲜 Worker 心跳，默认最大年龄为 `SRE_WORKER_HEARTBEAT_MAX_AGE_SECONDS=90`。监控 `sre_agent_change_workers{state="active|stale"}` 和 `sre_agent_change_jobs{status="queued|running|failed|unknown"}`；活跃 Worker 归零、队列持续增长、出现 unknown 或 running 超出租约时间必须告警。

执行器传输异常或 Worker 租约耗尽后状态为 `unknown`，表示外部动作可能已经发生。先在真实执行器中按变更 ID/幂等键核对，再由 admin 携带 Guard Token 调用 `/changes/{id}/redrive`。重驱保留原变更 ID 和幂等键并写入操作者、原因和次数；不得通过 SQL 直接把任务改回 queued。

生产默认 `SRE_PRODUCTION_WRITE_ENABLED=false`，只允许诊断和 dry-run。启用真实写操作前必须配置安全 allowlist 中的 Webhook executor、executor Token、Guard Token，并确认 `/health/ready` 的 `production_executor` 为 true；simulation 不能作为生产写执行器。

## 工作区密钥轮换与计量

生产首次启动使用环境变量 `SRE_ADMIN_API_KEY` 作为 Bootstrap 管理密钥。随后通过 `POST /workspace/api-keys` 创建工作区密钥，保存本次响应中的明文并验证 `/auth/me`，再轮换环境密钥。数据库只保存密钥摘要和前缀。

轮换管理员密钥时必须先创建并验证第二把 admin 密钥，再撤销旧密钥；接口会拒绝撤销最后一把有效的工作区 admin 密钥。每月检查 `/billing/usage`，额度耗尽后普通工作区请求返回 429，但 `/auth/me` 和 `/billing/usage` 仍可访问。

当前版本的安全边界是每客户独立部署、每实例单工作区。不要在同一实例中接入多个互不信任客户。

## 试用、续费与停用

trial 默认在首次初始化时根据 `SRE_TRIAL_DAYS` 固化到期时间，或采用明确的 `SRE_TRIAL_ENDS_AT`。邀请制免费试用设置 `SRE_TRIAL_START_MODE=activation`、`SRE_TRIAL_SELF_SERVICE_ENABLED=true` 和每客户唯一的高强度 `SRE_TRIAL_ACTIVATION_TOKEN`；新工作区先保持 `pending_activation`，领取事务成功后才开始计时并签发首把 admin Key。后续重启不会自动顺延。完整领取、密钥丢失恢复、反馈和隐私流程见 [FREE_TRIAL.md](FREE_TRIAL.md)。

管理员通过 `/trial/conversion-metrics` 检查领取、接入、首次查询、首次诊断、time-to-first-value、反馈与付费意向，通过 `/billing/subscription` 检查状态变更事件。试用到期、订阅暂停或付款宽限期结束后，业务 API 返回 402；`/auth/me`、`/workspace`、监控、`/billing/*` 与 `/trial/*` 保持可用，便于导出证据、提交反馈和恢复订阅。

当前升级仍为人工流程：取得明确联系授权后，通过 `SRE_UPGRADE_CONTACT_URL` 约定合同，再同时修改 Web 的 `SRE_PLAN`、`SRE_SUBSCRIPTION_STATUS=active` 与 `SRE_MONTHLY_REQUEST_LIMIT` 后重新部署。支付和自动开票尚未实现，不得对用户宣称在线付款已可用。付款逾期或周期末取消时，把状态设为 `past_due` 或 `canceled`，并用 `SRE_CURRENT_PERIOD_END` 指定 UTC 宽限边界。Web 与 Worker 必须使用完全相同的工作区和订阅配置；`render.yaml` 已通过 `fromService` 强制继承。完整状态语义见 [SUBSCRIPTION_LIFECYCLE.md](SUBSCRIPTION_LIFECYCLE.md)。

环境 Bootstrap Key 仅用于首次创建密钥和账户恢复。生产已有工作区密钥后，Bootstrap Key 不能调用服务、聊天、Incident 或变更业务接口；日常请求必须使用可撤销、可计量的工作区 Key。

模型价格通过 `SRE_LLM_INPUT_COST_PER_MILLION_USD` 和 `SRE_LLM_OUTPUT_COST_PER_MILLION_USD` 配置。每次成功响应会记录输入/输出 token 和当时估算成本；变更价格只影响新事件。合同月费和超额单价分别通过 `SRE_PLAN_PRICE_USD_MONTHLY`、`SRE_REQUEST_OVERAGE_USD_PER_1000` 配置。

每个 UTC 月结束后，管理员先调用 `/billing/statements/preview` 核对套餐、用量、超额和金额，再用唯一幂等键调用 `/billing/statements/finalize`。定稿快照不会因迟到事件或保留清理而变化；导出的 JSON/CSV 和 `payload_hash` 应一并写入财务归档。生产默认启用 `SRE_REQUIRE_FINALIZED_BILLING_BEFORE_USAGE_PURGE=true`，存在未定稿月份时拒绝删除用量。完整流程见 [BILLING_STATEMENTS.md](BILLING_STATEMENTS.md)。

## 数据库升级

部署新版本前先备份数据库，然后执行：

```bash
python -m backend.migrations upgrade --require-current
python -m backend.migrations status --require-current
```

迁移在 SQLite 使用写锁，在 PostgreSQL 使用 advisory lock，Web 与 Worker 并发启动时只有一个实例执行升级。迁移版本、名称和校验和不一致，或数据库版本高于应用支持版本时，启动必须失败；不得手工修改 `schema_migrations` 绕过检查。

## 数据保留与清理

先预览每类数据的截止时间和候选行数：

```bash
python -m backend.maintenance purge
```

核对账单导出、审计归档和备份完成后，才允许显式执行：

```bash
python -m backend.maintenance purge --apply --confirm PURGE:<workspace-id>
```

清理在单一事务中按子表到主表顺序执行；确认字符串必须与当前工作区完全一致。默认保留日志 30 天、会话 90 天、任务 180 天、用量 400 天、Incident/变更 730 天、试用激活失败 30 天、试用反馈 730 天、执行审计 7 年，可通过 `SRE_RETENTION_*_DAYS` 调整。试用激活事实不会随普通保留任务删除。生产用量清理还要求所有受影响月份已生成账单快照。审计清理只删除已过期的连续链前缀，并在同一事务写入链头 checkpoint；清理前若 `/audit/verify` 失败，操作会拒绝执行。归档必须同时保存导出的审计和当时的链头。

## 审计链校验

执行审计以 `audit_ledger` 为对外事实源，每条记录提交前一条哈希和规范化载荷。管理员应在每日归档、版本升级、数据保留清理和事故复盘前调用 `GET /audit/verify`，要求 `valid=true`，并把 `head_hash`、`entry_count`、`pruned_entry_count` 与归档时间写入客户侧 WORM/对象锁存储。

该机制可以发现孤立编辑、重排和中间删除，但哈希和数据仍在同一数据库；如果攻击者能重写整条链及其本地 checkpoint，单靠本库无法证明历史未被重建。生产合规必须使用外部不可变链头归档或后续接入带密钥签名的审计服务。

## 当前限制

- 未设置 `DATABASE_URL` 时回退到 SQLite，仅允许本地或单节点试点；生产和多副本部署必须使用 PostgreSQL。
- PostgreSQL 备份应使用托管数据库的 PITR/快照或 `pg_dump`，SQLite maintenance 命令会拒绝在 PostgreSQL 模式下运行。
- 审计记录已有可校验哈希链、操作者和变更关联，但外部不可变链头归档仍需客户存储或托管审计服务完成。
- Webhook 执行器必须由对端完成真实发布和健康验证后返回 `verified: true`。
- 已生成带哈希的不可变月度账单快照，但尚未接入支付网关、自动续费、税务/发票系统或加密签名的离线许可证；自托管客户的部署配置仍属于合同和运维控制边界。

## Render 托管拓扑

`render.yaml` 是生产最低拓扑：Starter Web、Starter Background Worker 和 Basic PostgreSQL。Web 在每次部署前执行受锁保护的 schema migration，CI 全绿后才自动部署；Web 与 Worker 共用 PostgreSQL，Worker 从 Web 服务引用同一套执行器 URL、Token、allowlist 和生产写开关。首次创建自助试用 Blueprint 时必须填写唯一的 `SRE_TRIAL_ACTIVATION_TOKEN`、至少 32 位的 `SRE_ADMIN_API_KEY`、有效的 `SRE_UPGRADE_CONTACT_URL` 和 `EXECUTION_GUARD_TOKEN`。试用 onboarding 阶段允许稍后接入真实只读数据源，但转为付费试点前必须配置并验证数据源，否则 readiness 会按设计拒绝上线。

Free Render Web 会休眠，Free PostgreSQL 会到期且没有备份，Background Worker 也不支持 Free 实例，因此 Free 资源只允许临时演示，不能替换该生产拓扑。启用真实写操作时必须同时核对 Web readiness、Worker 日志和真实执行器幂等验证。
