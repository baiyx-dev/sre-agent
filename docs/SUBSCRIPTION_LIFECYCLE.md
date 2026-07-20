# 试用与订阅生命周期

当前商业交付采用每客户独立部署。订阅配置由部署方控制并在应用启动时写入 PostgreSQL；每次状态、套餐、周期或额度变化都会追加一条 `subscription_events` 记录。该机制用于试点转付费和私有化合同执行，不是防篡改许可证，也不替代支付网关。

## 状态与访问规则

| 套餐/状态 | 业务 API | 恢复与导出接口 |
|---|---:|---:|
| trial + pending_activation | 未开放 | 可领取 |
| trial + trialing，未到期 | 开放 | 开放 |
| paid + active | 开放 | 开放 |
| paid + past_due，周期末之前 | 宽限开放 | 开放 |
| paid + canceled，周期末之前 | 开放至周期末 | 开放 |
| expired、suspended 或宽限期结束 | 返回 402 | 开放 |

恢复与导出接口包括 `/auth/me`、`/workspace`、监控接口、工作区密钥查询/撤销、`/billing/*` 和 `/trial/*`。已到期时不能创建新密钥。`/health/ready` 会报告订阅状态，但正常的到期不会让进程退出，否则客户无法完成续费和数据导出。

## 开始试用

```env
SRE_PLAN=trial
SRE_SUBSCRIPTION_STATUS=trialing
SRE_TRIAL_DAYS=14
SRE_TRIAL_ENDS_AT=
SRE_MONTHLY_REQUEST_LIMIT=1000
```

留空 `SRE_TRIAL_ENDS_AT` 时，首次初始化使用当前 UTC 时间加 `SRE_TRIAL_DAYS` 并持久化；重启不会重新计算。合同已有明确截止时间时使用 ISO-8601 UTC 时间，例如 `2026-08-03T00:00:00Z`。

邀请制免费试用使用 `SRE_TRIAL_START_MODE=activation`，领取成功后才计算截止时间；配置和用户流程见 [FREE_TRIAL.md](FREE_TRIAL.md)。

## 升级为付费套餐

```env
SRE_PLAN=team
SRE_SUBSCRIPTION_STATUS=active
SRE_MONTHLY_REQUEST_LIMIT=100000
SRE_TRIAL_ENDS_AT=
SRE_CURRENT_PERIOD_END=2026-09-01T00:00:00Z
```

Web 与 Worker 必须同时使用这些值。部署后检查：

```bash
curl -H "X-SRE-API-Key: $SRE_ADMIN_API_KEY" \
  "$SRE_URL/billing/subscription"
```

确认 `effective_status=active`、`access_allowed=true`，再用工作区 Key 调用一个只读服务接口。不要通过 SQL 修改套餐；这会绕过配置事件并在下次部署时被环境配置覆盖。

## 逾期、取消与恢复

逾期但允许使用到周期末：

```env
SRE_SUBSCRIPTION_STATUS=past_due
SRE_CURRENT_PERIOD_END=2026-09-01T00:00:00Z
```

周期末取消使用 `canceled` 和相同的周期边界；立即停用使用 `suspended`。付款恢复后改回 `active` 并部署。所有时间按 UTC 解释；无效状态或时间会让迁移/启动失败，而不是静默放行。

## 已知商业边界

- 尚未自动接收支付成功、退款、拒付或发票事件，状态变更仍由部署方执行。
- 自托管客户可以修改其运行代码或数据库，因此强制许可证需要后续引入签名授权或托管控制平面。
- 订阅事件与业务数据库同库，不等同于外部不可变财务账本。
