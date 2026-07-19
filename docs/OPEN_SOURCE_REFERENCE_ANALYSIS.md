# 开源 SRE/AIOps 项目参考分析

> 调研时间：2026-07-19。本文只采用项目官方仓库中能够验证的能力，不用 Star 数量代替产品成熟度判断。

## 结论

本项目不应该继续扩展成“能聊天、能调用几个接口”的通用 AI 助手。更有落地和收费可能的定位是：

**把监控证据、诊断建议、审批和变更执行串成可验证、可审计的 SRE 变更控制面。**

市场上的成熟项目已经分别覆盖诊断、Agent 编排、告警管理和自动化执行，但仍有机会把以下链路做成一个更轻、更安全的产品：

```text
告警/人工提问
  -> 确定性分析器提取证据
  -> AI 归因与建议
  -> 策略检查
  -> 人工审批
  -> 受控执行
  -> 发布后验证
  -> 成功关闭 / 自动回滚
```

## 参考项目与可复用模式

| 项目 | 已验证的成熟模式 | 本项目应借鉴 | 暂不照搬 |
|---|---|---|---|
| [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) | 面向生产可观测性与事故调查；默认只读；源端聚合、JSON 遍历、摘要转换、单工具内存限制与输出预算；多数据源和多模型 | 证据优先、只读默认、工具输出预算、发布验证、工具集契约 | 早期不追求几十种连接器；先把 3 个关键数据源做深 |
| [K8sGPT](https://github.com/k8sgpt-ai/k8sgpt) | 用分析器固化 SRE 诊断经验，再用模型解释；支持多模型与 MCP | 将确定性分析和 LLM 推理解耦；分析结果先结构化、脱敏，再送入模型 | 不把产品限制为 Kubernetes 单一场景 |
| [Kagent](https://github.com/kagent-dev/kagent) | Kubernetes 原生 Agent/ModelConfig/ToolServer 资源；MCP 工具复用；OpenTelemetry；声明式、可测试 | 工具能力清单、声明式配置、模型适配层、全链路 tracing | 当前阶段不自研 Kubernetes Operator/CRD 控制器 |
| [Keep](https://github.com/keephq/keep) | 告警聚合、去重、关联、富化、双向集成和工作流；Provider Factory；只读模式；Secret Manager 与租户上下文 | Provider 插件契约、告警实体、去重/关联、密钥后端、租户贯穿调用链 | 不先做大而全的告警平台和连接器市场 |
| [Rundeck](https://github.com/rundeck/rundeck) | Runbook Automation；标准化任务；Web/CLI/API；执行历史；成熟的开源到企业商业路径 | Job/Runbook 模型、执行器适配、ACL、执行日志、企业交付形态 | 不在本项目里重造完整作业调度平台；优先对接它或 Argo CD |
| [StackStorm](https://github.com/StackStorm/st2) | Event→Trigger→Rule→Workflow→Action；Packs；失败时冻结并转人工；完整审计；松耦合服务与消息总线 | 事件规则模型、异步工作流、失败接管、可安装能力包、不可变审计 | M0/M1 不拆成大量微服务，也不开放任意脚本执行 |

## 对现有实现的影响

现有代码已完成的方向是正确的：API Key 角色权限、默认关闭直接执行、服务端变更请求、防篡改/防重放、SSRF 防护、敏感字段不回显、前端 XSS 加固。这些是“安全控制面”的起点。

参考成熟项目后，后续架构应按以下顺序演进：

1. **统一能力契约**：每个工具声明 `read_only`、所需角色、输入 schema、超时、最大输出、允许访问的目标和审计字段。
2. **分析器流水线**：先由可测试的分析器收集指标、日志、Kubernetes 事件和变更记录，再由 LLM 解释；原始大数据不直接进入提示词。
3. **事故领域模型**：Incident 不再只是一次对话或 task run，应包含告警归并、时间线、证据、负责人、状态、影响范围和复盘。
4. **完整变更状态机**：`pending_policy -> pending_approval -> queued -> running -> verifying -> succeeded/failed/rolled_back/cancelled`，所有迁移都要原子化且可审计。
5. **执行器适配层**：保留 simulation，同时接入一个真实执行器。优先级建议是 Argo CD，其次是 Rundeck/StackStorm，而不是直接在 API 进程执行任意命令。
6. **异步与可靠性**：PostgreSQL + 队列/Worker + 幂等键 + outbox + 重试/死信 + 明确的未知执行状态处理。
7. **平台能力**：Tenant/User/Role、Secret Manager、审计导出、OpenTelemetry、配额、用量计量和数据保留策略。

## 可收费版本边界

| 版本 | 核心价值 | 建议边界 |
|---|---|---|
| Community | 获客与验证 | 单租户、自托管、只读诊断、有限连接器、社区支持 |
| Team | 降低团队 MTTR | 托管版、告警归并、协作时间线、常用数据源、受控 dry-run、基础审计 |
| Enterprise | 安全执行与合规 | SSO/SCIM、细粒度 RBAC、多级审批、私有化、审计归档、真实执行器、SLA |

更合理的计费维度是“平台基础费 + 活跃服务数/连接器数 + 自动化执行量 + 数据保留期”，不建议按聊天次数计费。客户购买的是缩短 MTTR、降低变更风险和满足审计，而不是 token。

## 落地优先级

### P0：可进入真实只读试点

- 一个真实 Prometheus、Loki 和 Kubernetes 环境的端到端接入。
- 结构化分析器、工具输出上限、超时与脱敏。
- PostgreSQL、租户隔离、Secret Manager 和完整审计。
- Incident 时间线以及诊断结果的可追溯证据链接。

### P1：可进入测试环境变更试点

- 完整变更状态机、异步 Worker 和一个真实执行器。
- 多级审批、取消、重试、发布后健康验证和自动回滚。
- OpenTelemetry、SLO、告警、备份恢复与故障演练。

### P2：可商业交付

- SSO/SCIM、套餐权限、配额/计量、审计归档和私有化交付。
- 安装、升级、回滚、运维和客户成功手册。
- 至少 3 个设计合作客户，用数据验证 MTTR、建议采纳率、执行成功率和付费意愿。

## 明确不做

- 不让 LLM 直接拼接并执行 Shell/Kubectl 命令。
- 不用前端传入的动作内容作为审批后的执行依据。
- 不把“支持很多连接器”当作早期核心指标。
- 不在缺少真实客户数据前承诺无人值守自动修复。
- 不先建设完整微服务体系；先验证可靠链路与付费价值，再按容量拆分。
