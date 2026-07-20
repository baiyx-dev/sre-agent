# SRE Agent

SRE Agent 是一个面向服务运维场景的 AI Copilot 项目。

它以自然语言交互为入口，把意图识别、工具调用、观测数据聚合、执行前策略评估、任务持久化和复盘能力整合在一起，用于完成常见的服务状态查询、故障排查、变更前评估、部署/回滚确认和 incident 回放评测。

项目目标不是让模型直接替代运维系统，而是提供一条可控、可追踪、可回放的运维协作链路：模型负责理解和生成，系统负责取数、约束、审计和兜底。

当前版本的能力重点在运维分析、诊断辅助和执行前决策支持；对于 deploy / rollback，系统已经具备策略评估、dry-run、确认和审计能力，但最后一步还没有对接真实发布平台。

产品化路线见 [PRODUCTIZATION_ROADMAP.md](docs/PRODUCTIZATION_ROADMAP.md)，开源参考见 [OPEN_SOURCE_REFERENCE_ANALYSIS.md](docs/OPEN_SOURCE_REFERENCE_ANALYSIS.md)，首个客户交付按 [PILOT_LAUNCH_CHECKLIST.md](docs/PILOT_LAUNCH_CHECKLIST.md) 验收，价值证据按 [PILOT_VALUE_REPORTING.md](docs/PILOT_VALUE_REPORTING.md) 记录和导出。

进程级烟测和当前并发数据见 [PERFORMANCE_BASELINE.md](docs/PERFORMANCE_BASELINE.md)。


## 功能概览

- 自然语言运维入口
  - 支持状态查询、故障排查、部署、回滚
  - 支持普通表达和跟进式对话，不要求固定关键词
- 多轮会话与澄清
  - 记住最近一次服务、环境、版本等上下文
  - 在缺失目标版本或服务名时进入澄清流程
- 证据驱动故障诊断
  - 聚合 alerts、status、metrics、logs、deployment context
  - Agent 基于 metrics / logs / alerts / K8s 运行态进行故障分析与风险评估
  - 输出风险等级、关键证据、根因候选、缺失信号和下一步动作
- 高风险操作控制
  - deploy / rollback 支持 dry-run
  - 支持策略评估、服务端变更请求、确认执行和审计留痕
  - 变更请求默认 15 分钟过期，并通过原子状态转换阻止篡改和重复执行
- 观测数据接入
  - 支持统一 SRE API
  - 支持 Prometheus、Loki
  - 支持 K8s rollout、pod、event 级观测
  - 支持自定义 PromQL / Loki 查询模板
- 时间线与复盘
  - 保存每次任务、步骤和结果
  - 支持 timeline 和结构化 postmortem
- 日志与异常处理
  - 记录请求日志、错误日志和 request id
  - 提供统一异常返回格式
- 内部运行指标
  - 提供成功率、错误率、平均响应时间和 P95 响应时间
  - 支持 Prometheus 风格导出接口 `/metrics`
- Incident replay / benchmark
  - 内置基线故障场景
  - 支持单场景回放和批量 benchmark
  - 支持对意图、澄清、证据、根因、下一步动作等维度自动评分


## 系统组成

### 1. Chat Orchestrator

聊天请求首先经过意图识别和实体抽取，再由编排层按场景调用工具链。

主要负责：

- 判断用户意图
- 提取服务名、环境、版本、时间窗口等实体
- 处理澄清流程和多轮上下文
- 编排状态查询、排障、部署、回滚等任务


### 2. Tool Layer

工具层负责从不同来源获取结构化事实数据。

已支持：

- 服务状态
- 指标
- 日志
- 告警
- 最近部署上下文
- 主动探测
- Prometheus / Loki
- Kubernetes deployment、pods、events


### 3. Policy Layer

执行类动作不会直接落到写操作，而是先走执行前评估。

当前评估会综合：

- 服务是否存在
- 当前状态和错误率
- 开放告警
- Deployment rollout 状态
- Pod 健康和重启情况
- Warning 事件
- 部署目标版本或回滚历史是否满足前置条件


### 4. Storage Layer

设置 `DATABASE_URL` 时项目使用 PostgreSQL，供 Web 与 Worker 共享事务和耐久队列；未设置时回退到仅供本地/单节点试点的 SQLite。存储内容包括：

- 服务、日志、告警、部署历史
- 任务运行记录和步骤明细
- 执行审计
- 会话上下文
- 应用设置
- 主动监测目标


### 5. Evaluation Layer

benchmark 用于回放固定 incident 场景，验证系统在关键路径上的稳定性。

当前评测覆盖：

- intent 命中
- confirmation / clarification 命中
- severity 命中
- answer keyword 命中
- evidence 命中
- hypothesis 命中
- next action 命中
- policy recommended mode 命中


### 6. Logging And Runtime Metrics

应用在 HTTP 入口增加了统一的请求观测层。

当前会记录：

- `request_id`
- 请求方法和路径
- 状态码
- 请求耗时
- HTTPException 和未处理异常

同时提供内部运行指标接口，用于查看：

- 请求总数
- 成功率 / 错误率
- 平均响应时间
- P95 响应时间
- 运行时长

当前还提供 Prometheus 风格指标导出，便于接入外部监控系统。


## 技术栈

- Backend: `FastAPI`, `Pydantic`, `PostgreSQL`（SQLite 本地回退）
- Frontend: 原生 `HTML + CSS + JavaScript`
- LLM: `DeepSeek` 兼容接口
- Observability: `Prometheus`, `Loki`, `Kubernetes API`


## 目录结构

```text
sre-agent/
├── backend/
│   ├── agents/      # 意图识别、实体抽取、任务编排
│   ├── api/         # FastAPI 路由
│   ├── llm/         # LLM provider 封装
│   ├── schemas/     # 请求/响应模型
│   ├── services/    # policy、benchmark 等服务层
│   ├── storage/     # PostgreSQL/SQLite 兼容连接、初始化与仓储
│   └── tools/       # status/metrics/logs/alerts/deploy/rollback/probe 等工具
├── frontend/        # 前端页面
├── tests/           # 回归测试
├── .env.example
├── requirements.txt
└── sre_agent.db
```


## 工作流程

1. 用户通过前端发送自然语言请求
2. 系统识别意图并抽取服务名、版本等实体
3. 如果关键信息不足，进入澄清流程
4. 编排层根据意图调用对应工具
5. 工具层返回结构化事实数据
6. 规则层和 LLM 共同生成结果
7. 如果模型不可用，自动回退到规则结果
8. 请求日志、异常和运行指标会在入口统一记录
9. 本次任务、步骤、评估和执行记录落到 PostgreSQL/SQLite
10. 历史结果可通过 timeline、postmortem 和 benchmark 回看


## 日志系统说明

日志系统当前分为两层：

### 1. 请求日志

每个 HTTP 请求都会记录：

- `request_id`
- `method`
- `path`
- `status_code`
- `duration_ms`

这部分用于排查接口耗时、失败路径和用户请求链路。

### 2. 异常日志

对于业务异常和未处理异常，系统会统一记录：

- 请求标识
- 异常状态码
- 异常详情
- 请求路径

未处理异常会额外写入完整堆栈，便于定位问题。


## 异常处理流程

系统当前采用统一异常处理流程：

1. 请求进入中间件后生成 `request_id`
2. 正常请求记录响应状态码和耗时
3. 业务异常通过 `HTTPException` 返回统一 JSON 结构
4. 未处理异常由全局异常处理器兜底，返回 `500`
5. 所有异常响应都会带上 `request_id`，便于在日志里定位

统一错误响应示例：

```json
{
  "error": "request_failed",
  "detail": "service not found",
  "request_id": "7d5b8d1e-0c9d-4c8e-a5e2-5f0d0b3e51d1"
}
```


## 运行指标

项目提供内部运行指标接口：

- `GET /internal/metrics`
- `GET /metrics`

返回内容包括：

- `request_count`
- `success_count`
- `error_count`
- `success_rate_pct`
- `error_rate_pct`
- `avg_response_time_ms`
- `p95_response_time_ms`
- `uptime_seconds`

Prometheus 风格导出当前包含：

- `sre_agent_request_total`
- `sre_agent_success_rate_pct`
- `sre_agent_avg_response_time_ms`
- `sre_agent_p95_response_time_ms`

多实例场景下，指标聚合层支持扩展为 Redis / 外部存储汇总模式，用于统一收敛多个 Agent 实例的运行指标。


## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```


### 2. 配置环境变量

参考 `.env.example`：

```env
DEEPSEEK_API_KEY=
DEEPSEEK_API_BASE=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
SRE_ENVIRONMENT=development
DATABASE_URL=postgresql://sre_agent:password@postgres:5432/sre_agent
# Production readiness fails closed when PostgreSQL is absent.
SRE_ALLOW_PRODUCTION_SQLITE=false
SRE_SEED_DEMO_DATA=true
SRE_REQUIRE_REAL_DATA_SOURCE=false
SRE_WORKSPACE_ID=default
SRE_WORKSPACE_NAME=Default workspace
SRE_PLAN=trial
SRE_MONTHLY_REQUEST_LIMIT=1000
SRE_SUBSCRIPTION_STATUS=trialing
SRE_TRIAL_DAYS=14
SRE_TRIAL_START_MODE=deployment
SRE_TRIAL_SELF_SERVICE_ENABLED=false
SRE_TRIAL_ACTIVATION_TOKEN=
SRE_UPGRADE_CONTACT_URL=
SRE_TRIAL_ENDS_AT=
SRE_CURRENT_PERIOD_END=

SRE_AUTH_ENABLED=true
SRE_VIEWER_API_KEY=
SRE_OPERATOR_API_KEY=
SRE_ADMIN_API_KEY=replace_with_a_long_random_secret
SRE_ALLOW_INSECURE_DB_SECRETS=false

SRE_DATA_API_BASE=
SRE_DATA_API_TOKEN=
SRE_OUTBOUND_HOST_ALLOWLIST=127.0.0.1,localhost
SRE_ALLOW_PRIVATE_NETWORK_TARGETS=false

PROMETHEUS_BASE_URL=
PROMETHEUS_TOKEN=
PROMETHEUS_SERVICE_LABEL=service
PROM_QUERY_UP=sum(up{service_selector})
PROM_QUERY_REPLICAS=count(up{service_selector})
PROM_QUERY_ERROR_RATE=100 * sum(rate(http_requests_total{service_selector_with_status_5xx}[5m])) / clamp_min(sum(rate(http_requests_total{service_selector}[5m])), 0.001)
PROM_QUERY_CPU=100 * avg(rate(process_cpu_seconds_total{service_selector}[5m]))
PROM_QUERY_MEMORY=avg(process_resident_memory_bytes{service_selector}) / 1024 / 1024
PROM_QUERY_LATENCY_P95_MS=1000 * histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{service_selector}[5m])) by (le))
PROM_ALERT_QUERY=ALERTS{alertstate="firing",service="{service_name}"}

LOKI_BASE_URL=
LOKI_TOKEN=
LOKI_SERVICE_LABEL=service
LOKI_QUERY_TEMPLATE={{{label}="{service_name}"}}

K8S_API_BASE=
K8S_API_TOKEN=
K8S_NAMESPACE=default
K8S_SERVICE_LABEL=app

EXECUTION_GUARD_ENABLED=true
EXECUTION_GUARD_TOKEN=
SRE_CHANGE_EXECUTOR=simulation
SRE_CHANGE_EXECUTOR_WEBHOOK_URL=
SRE_CHANGE_EXECUTOR_TOKEN=
SRE_CHANGE_EXECUTOR_TIMEOUT_SECONDS=30
SRE_CHANGE_EXECUTION_MODE=synchronous
SRE_CHANGE_JOB_MAX_ATTEMPTS=1
```

说明：

- 不配置 `DEEPSEEK_API_KEY` 也可以运行，系统会回退到规则结果
- API 认证默认开启；至少配置一个高强度 API Key，管理员 Key 同时拥有 viewer/operator 权限
- 当前商业交付采用每客户独立部署、每实例一个工作区；工作区 API Key 只保存 SHA-256 摘要，可撤销并按月计量
- 开发环境默认写入演示服务；生产环境默认 `SRE_SEED_DEMO_DATA=false`，不会把演示数据当成客户数据
- 生产环境默认 `SRE_REQUIRE_REAL_DATA_SOURCE=true`；至少配置统一 SRE API、Prometheus、Loki、Kubernetes API 或一个受监控目标且地址通过 SSRF 校验，readiness 才会通过。唯一例外是启用自助领取的 trial onboarding 宽限；升级为付费套餐后恢复严格门禁
- `SRE_REQUIRE_REAL_DATA_SOURCE=false` 只用于隔离评估，不能作为付费试点或生产验收依据
- `SRE_MONTHLY_REQUEST_LIMIT=0` 表示不限请求；trial/starter/team 的建议默认值分别为 1,000/10,000/100,000
- trial 默认在首次初始化时固化到期时间；邀请制试用可设置 `SRE_TRIAL_START_MODE=activation`，领取后才开始计时并签发首把 admin 工作区 Key。两种模式重启都不会延期，详见 [FREE_TRIAL.md](docs/FREE_TRIAL.md)
- 到期、暂停或超过付款宽限期后，业务 API 返回 402，但身份、工作区、监控、账单、价值报告和试用反馈仍可访问。升级与续费流程见 [SUBSCRIPTION_LIFECYCLE.md](docs/SUBSCRIPTION_LIFECYCLE.md)
- 套餐权限由服务端强制执行：trial/starter 只允许诊断和 dry-run，team/enterprise 才能在其余生产安全门禁全部通过后执行真实变更；工作区密钥上限依次为 3/10/50/不限
- 生产环境的 Bootstrap Key 在创建工作区密钥后只允许账户恢复与商业控制面调用，日常业务必须使用可计量的工作区 Key，避免绕过额度
- 前端只把 API Key 保存在当前浏览器标签页的 `sessionStorage`，关闭标签页后自动清除
- 数据源 Token 默认只从环境变量、Kubernetes Secret 或外部密钥管理器读取，不会写入 SQLite
- `SRE_ALLOW_INSECURE_DB_SECRETS=true` 仅供隔离的本地演示；生产环境必须保持 `false`
- 执行保护默认开启；要执行 deploy / rollback，必须配置 `EXECUTION_GUARD_TOKEN` 并在确认请求中提供 `X-Guard-Token`
- 只有隔离的演示或测试环境才应显式设置 `EXECUTION_GUARD_ENABLED=false`
- 生产真实写操作还必须显式设置 `SRE_PRODUCTION_WRITE_ENABLED=true`；此时 readiness 强制要求安全的 Webhook executor 和 executor Token，simulation 不会被当作生产执行器
- 优先读取统一 SRE API；未提供时，会尝试 Prometheus / Loki / K8s
- 查询模板可按团队的 metric / label 规范调整
- 所有前端配置的外部地址都会经过 SSRF 检查；内网地址必须加入 `SRE_OUTBOUND_HOST_ALLOWLIST`
- 不建议开启 `SRE_ALLOW_PRIVATE_NETWORK_TARGETS`，生产环境应使用精确域名白名单
- `SRE_CHANGE_EXECUTOR=simulation` 只修改演示数据库；测试环境可切换为 `webhook` 对接 Argo CD、Rundeck 或企业发布平台
- Webhook 请求带稳定的 `Idempotency-Key` 和变更请求 ID；响应必须同时返回 `success: true` 与 `verified: true`，否则系统按失败处理
- 长时间变更可设置 `SRE_CHANGE_EXECUTION_MODE=queued`，然后独立运行 `python -m backend.worker`；审批只负责入队，Worker 使用持久化任务、租约和原子领取避免并发重复执行
- 自动重试默认关闭（`SRE_CHANGE_JOB_MAX_ATTEMPTS=1`）；只有执行器严格支持幂等键时才应提高次数
- Worker 在传输异常或租约耗尽时标记 `unknown`，不会伪装成确定失败；admin 可在核对执行器后受控重驱，默认最多 3 次
- 确定性 Analyzer 会先收集并裁剪证据，再交给 LLM；`SRE_EVIDENCE_MAX_ITEMS`、`SRE_EVIDENCE_MAX_TEXT_CHARS` 和 `SRE_EVIDENCE_MAX_TOTAL_BYTES` 同时控制上下文、延迟和单次诊断成本
- 配置 `SRE_LLM_INPUT_COST_PER_MILLION_USD` 与 `SRE_LLM_OUTPUT_COST_PER_MILLION_USD` 后，每次成功模型调用会固化 token 和美元微成本；价格变化不会回写历史账目


### 3. 启动服务

```bash
uvicorn backend.main:app --reload --port 8000
```

启动后访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

也可以使用容器启动 PostgreSQL + Web + 耐久 Worker 的完整本地环境：

```bash
docker compose up --build
```

生产镜像以非 root 用户运行，并使用 `/health/ready` 作为容器健康检查。`compose.yaml` 中关闭认证和执行保护的配置只适用于本机演示；Web 和 Worker 通过 PostgreSQL 共享变更队列。

单节点试点的备份、恢复和队列 Worker 操作见 [运维手册](docs/OPERATIONS_RUNBOOK.md)。


## 部署到 Render

仓库已包含 `render.yaml`，可以直接按 Blueprint 方式部署。

### 1. 推送代码到 GitHub

将当前项目推送到你的 GitHub 仓库。

### 2. 在 Render 创建 Blueprint

1. 打开 Render
2. 选择 `New +`
3. 选择 `Blueprint`
4. 连接当前 GitHub 仓库
5. Render 会自动识别仓库根目录下的 `render.yaml`

### 3. 配置环境变量

建议至少配置：

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_API_BASE`
- `DEEPSEEK_MODEL`

可选配置：

- `SRE_DATA_API_BASE`
- `SRE_DATA_API_TOKEN`
- `PROMETHEUS_BASE_URL`
- `PROMETHEUS_TOKEN`
- `LOKI_BASE_URL`
- `LOKI_TOKEN`
- `K8S_API_BASE`
- `K8S_API_TOKEN`
- `EXECUTION_GUARD_ENABLED`
- `EXECUTION_GUARD_TOKEN`

说明：

- `render.yaml` 会创建 PostgreSQL 并通过 `DATABASE_URL` 注入连接串
- 未设置 `DATABASE_URL` 时才会回退 SQLite；该模式不支持多实例高可用
- 如果不配置外部观测系统，应用仍可用内置基线数据启动

### 4. 完成部署

Render 会执行：

- `pip install -r requirements.txt`
- `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`

部署完成后，直接访问 Render 分配的公网地址即可。


## 使用方式

### 1. 直接体验内置数据

项目启动时会自动初始化一组基线数据，不接外部系统也可以直接体验：

- 服务状态查询
- 故障排查
- 回滚确认
- 时间线
- postmortem
- benchmark


### 2. 接入外部观测系统

支持三类数据来源：

- 统一 SRE API
- Prometheus + Loki
- Kubernetes API

如果已有外部系统，可以在前端配置页中填写对应地址和 token，再测试连接。


### 3. 只接入单个服务地址

如果没有统一观测接口，也可以只填写服务名和健康检查地址。

系统会：

- 保存该目标
- 主动探测连通性
- 为聊天和状态查询提供一个最小可用的数据入口


## 示例请求

```text
payment-service 状态
帮我看看 payment-service 最近是不是有问题
回滚 payment-service
部署 payment-service
那就回滚吧
```


## Benchmark 接口

- `GET /benchmark/scenarios`
  - 查看当前场景集
- `GET /benchmark/replay/{scenario_id}`
  - 回放单个场景
- `GET /benchmark/run`
  - 批量执行全部场景并返回汇总评分

当前基线场景包括：

- 服务状态查询
- 自然语言故障排查
- 高风险回滚确认
- 缺失参数时的 deploy 澄清


## API 概览

### Chat

- `POST /chat`
- `POST /chat/confirm`

### Services / Incidents

- `GET /services/`
- `GET /services/{service_name}`
- `GET /services/{service_name}/metrics`
- `GET /services/{service_name}/logs`
- `GET /alerts`
- `GET /incidents`：查询正式 Incident 列表，可按状态和服务过滤
- `GET /incidents/{incident_id}`：查询归并告警与完整事件时间线
- `POST /incidents/correlate`：把当前未恢复告警确定性归并为 Incident
- `PATCH /incidents/{incident_id}`：设置负责人、摘要并按状态机推进 Incident
- `POST /deploy`：创建部署变更请求；除 `dry_run=true` 外不会直接执行
- `POST /rollback`：创建回滚变更请求；除 `dry_run=true` 外不会直接执行
- `GET /changes/{change_request_id}`：查询变更请求及最终结果
- `GET /changes`：按状态查询变更请求列表
- `POST /changes/{change_request_id}/confirm`：确认并原子领取一次变更请求，支持 `dry_run`
- `POST /changes/{change_request_id}/cancel`：在 pending/queued 阶段取消变更
- `POST /changes/{change_request_id}/redrive`：admin 对 failed/unknown 死信任务做带 Guard Token 的幂等重驱
- `GET /audit/executions`：管理员查询可过滤、可用 `before_sequence` 游标分页归档的哈希链执行审计，包含事件 ID、申请人、审批人关联信息和链哈希
- `GET /audit/verify`：管理员校验完整审计链并取得当前链头；应把链头定期写入客户侧不可变存储
- `GET /health/live`：进程存活探针
- `GET /health/ready`：数据库、认证、执行保护和密钥模式就绪探针；生产模式默认要求 PostgreSQL，配置不安全时返回 503
- 每个响应包含 `X-Request-Id`、`X-Trace-Id` 和 W3C `traceparent`；生产默认 JSON 结构化日志，可通过 `SRE_LOG_LEVEL` 控制级别
- `GET /metrics`：Prometheus 文本指标，需要 viewer 或更高权限
- `GET /timeline`
- `GET /postmortem?task_run_id=...`

### Benchmark

- `GET /benchmark/scenarios`
- `GET /benchmark/replay/{scenario_id}`
- `GET /benchmark/run`

### Internal

- `GET /internal/metrics`
- `GET /metrics`

### Settings

- `GET /settings/data-source`
- `PUT /settings/data-source`
- `POST /settings/data-source/test`
- `GET /settings/targets`
- `POST /settings/targets`
- `DELETE /settings/targets/{name}`

### Workspace / Billing

- `GET /trial/status`：公开查询该独立实例是否可领取，不返回工作区或联系人信息
- `POST /trial/activate`：使用邀请令牌一次性启动试用并领取首把 admin 工作区 Key
- `GET /trial/onboarding`：查看接入、首次查询、首次诊断和反馈里程碑
- `POST /trial/feedback`：幂等提交评分、价值结果、付费意向和缺失能力
- `GET /trial/conversion-metrics`：管理员查看首次价值时间和试用转化证据

- `GET /workspace`：查看当前隔离工作区、套餐与请求额度
- `GET /workspace/api-keys`：管理员查看密钥元数据，不返回密钥或摘要
- `POST /workspace/api-keys`：创建可撤销的工作区密钥；明文只在本次响应返回
- `DELETE /workspace/api-keys/{key_id}`：撤销密钥；系统拒绝撤销最后一个工作区管理员密钥
- `GET /billing/usage`：查询 UTC 自然月的持久化请求用量、额度与剩余额度
- `GET /billing/subscription`：查询试用/付费状态、剩余天数和配置变更事件
- `GET /billing/usage.csv?month=YYYY-MM`：管理员导出逐事件用量、token、成本和调用元数据
- `GET /billing/statements/preview?month=YYYY-MM`：管理员按当前合同价格预览月度账单
- `POST /billing/statements/finalize`：管理员幂等冻结已结束月份的用量、金额和校验哈希
- `GET /billing/statements`、`GET /billing/statements/{month}`：查询已定稿账单
- `GET /billing/statements/{month}/verify`：重新计算规范化快照哈希并检查完整性
- `GET /billing/statements/{month}/export.csv`：导出稳定的开票底稿
- `POST /billing/pilot-outcomes`：管理员按幂等键记录节省时间、建议采纳、结果成功和支持工时
- `GET /billing/pilot-outcomes`：管理员查询指定月份或日期范围内的原始价值证据
- `GET /billing/value-report`：管理员汇总活动、MTTR、变更结果、价值与单位经济性
- `GET /billing/value-report.csv`：管理员导出单行周报/月报；支持 `month` 或 `start_date`/`end_date`

动态用量只能用于检查，不能直接作为长期不变的开票事实。每个 UTC 月结束后按 [BILLING_STATEMENTS.md](docs/BILLING_STATEMENTS.md) 先预览再定稿；生产数据保留默认拒绝清理尚未定稿月份的用量。


## 外部数据源约定

生产启动不会自动灌入演示服务，并由 `/health/ready` 的 `real_data_source` 检查阻止“空壳正常”。该检查验证至少一个数据源或受监控目标已经配置且 URL 安全；数据源的真实权限、连通性和数据质量仍需使用设置页测试与试点验收脚本验证。

如果接入统一 SRE API，推荐提供以下接口：

- `GET /services`
- `GET /services/{service_name}`
- `GET /metrics/{service_name}`
- `GET /logs?service_name=...&limit=...`
- `GET /alerts?service_name=...&unresolved_only=true&limit=...`
- `GET /k8s/observability/{service_name}`

最小可用版本至少提供：

- `GET /services`

示例返回：

```json
{
  "services": [
    {
      "service_name": "payment-service",
      "base_url": "https://api.example.com",
      "status": "running",
      "error_rate": 0.02
    }
  ]
}
```


## 持久化数据

PostgreSQL（或本地 SQLite 回退）中主要保存以下内容：

- `services`
- `alerts`
- `logs`
- `deployments`
- `task_runs`
- `task_steps`
- `execution_audits`
- `audit_ledger`
- `audit_ledger_checkpoints`
- `chat_sessions`
- `app_settings`
- `monitored_targets`
- `change_requests`
- `change_jobs`
- `incidents`
- `incident_alerts`
- `incident_events`


## 当前限制

- 内置 simulation 只用于演练；生产写操作必须把 Webhook executor 对接到 Argo CD、Rundeck 或企业发布平台
- 主动探测模式提供的是最小可用观测，不等同于真实监控系统
- 已支持环境 Bootstrap Key 与持久化、可撤销的工作区 API Key RBAC；尚未完成 OIDC/SSO、SCIM 和共享数据库的行级多租户隔离
- 当前商业安全边界是“每客户独立部署、单工作区”，不能把多个互不信任客户放入同一实例
- PostgreSQL 已加入兼容层和 CI 服务测试，但上线前仍需在目标托管数据库完成容量与故障演练
- 数据库采用带校验和与并发锁的前向迁移；部署前运行 `python -m backend.migrations upgrade --require-current`，不支持直接用旧版本程序读取更新后的数据库
- `python -m backend.maintenance purge` 默认只预览保留策略；实际清理要求 `--apply --confirm PURGE:<workspace-id>` 并在单一事务中执行
- 前端仍是轻量原生实现，没有组件化框架


## License

当前仓库未单独声明 License，如需开源发布，建议补充相应许可证。
