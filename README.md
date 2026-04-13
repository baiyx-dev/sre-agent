# SRE Agent

SRE Agent 是一个面向服务运维场景的 AI Copilot 项目。

它以自然语言交互为入口，把意图识别、工具调用、观测数据聚合、执行前策略评估、任务持久化和复盘能力整合在一起，用于完成常见的服务状态查询、故障排查、变更前评估、部署/回滚确认和 incident 回放评测。

项目目标不是让模型直接替代运维系统，而是提供一条可控、可追踪、可回放的运维协作链路：模型负责理解和生成，系统负责取数、约束、审计和兜底。

当前版本的能力重点在运维分析、诊断辅助和执行前决策支持；对于 deploy / rollback，系统已经具备策略评估、dry-run、确认和审计能力，但最后一步还没有对接真实发布平台。


## 功能概览

- 自然语言运维入口
  - 支持状态查询、故障排查、部署、回滚
  - 支持普通表达和跟进式对话，不要求固定关键词
- 多轮会话与澄清
  - 记住最近一次服务、环境、版本等上下文
  - 在缺失目标版本或服务名时进入澄清流程
- 证据驱动故障诊断
  - 聚合 alerts、status、metrics、logs、deployment context
  - 输出风险等级、关键证据、根因候选、缺失信号和下一步动作
- 高风险操作控制
  - deploy / rollback 支持 dry-run
  - 支持策略评估、确认执行和审计留痕
- 观测数据接入
  - 支持统一 SRE API
  - 支持 Prometheus、Loki
  - 支持 K8s rollout、pod、event 级观测
  - 支持自定义 PromQL / Loki 查询模板
- 时间线与复盘
  - 保存每次任务、步骤和结果
  - 支持 timeline 和结构化 postmortem
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

项目使用 SQLite 存储运行状态和历史记录，包括：

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


## 技术栈

- Backend: `FastAPI`, `Pydantic`, `SQLite`
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
│   ├── storage/     # SQLite 初始化与仓储
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
8. 本次任务、步骤、评估和执行记录落到 SQLite
9. 历史结果可通过 timeline、postmortem 和 benchmark 回看


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

SRE_DATA_API_BASE=
SRE_DATA_API_TOKEN=

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

EXECUTION_GUARD_ENABLED=false
EXECUTION_GUARD_TOKEN=
```

说明：

- 不配置 `DEEPSEEK_API_KEY` 也可以运行，系统会回退到规则结果
- 优先读取统一 SRE API；未提供时，会尝试 Prometheus / Loki / K8s
- 查询模板可按团队的 metric / label 规范调整


### 3. 启动服务

```bash
uvicorn backend.main:app --reload --port 8000
```

启动后访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。


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
- `POST /deploy`
- `POST /rollback`
- `GET /timeline`
- `GET /postmortem?task_run_id=...`

### Benchmark

- `GET /benchmark/scenarios`
- `GET /benchmark/replay/{scenario_id}`
- `GET /benchmark/run`

### Settings

- `GET /settings/data-source`
- `PUT /settings/data-source`
- `POST /settings/data-source/test`
- `GET /settings/targets`
- `POST /settings/targets`
- `DELETE /settings/targets/{name}`


## 外部数据源约定

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

SQLite 中主要保存以下内容：

- `services`
- `alerts`
- `logs`
- `deployments`
- `task_runs`
- `task_steps`
- `execution_audits`
- `chat_sessions`
- `app_settings`
- `monitored_targets`


## 当前限制

- `deploy / rollback` 当前实现的是受控演练型执行链路，包含策略评估、dry-run、确认和审计，但尚未对接真实发布系统
- 主动探测模式提供的是最小可用观测，不等同于真实监控系统
- 没有用户体系、RBAC 和审批流
- 前端仍是轻量原生实现，没有组件化框架


## License

当前仓库未单独声明 License，如需开源发布，建议补充相应许可证。
