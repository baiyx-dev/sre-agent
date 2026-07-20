# 付费试点价值报告

这套报告把“系统有功能”转成可复算的客户价值与单位经济性证据。它适用于当前的每客户独立部署、单工作区交付边界，不替代客户签字、财务账单或生产 SLO 报告。

## 配置成本假设

```env
SRE_PLAN_PRICE_USD_MONTHLY=1000
SRE_INFRA_COST_USD_MONTHLY=100
SRE_CUSTOMER_HOURLY_COST_USD=100
SRE_SUPPORT_HOURLY_COST_USD=50
```

- 合同月费与月基础设施成本按报告天数除以 30.4375 折算。
- 客户时薪应使用含福利、管理和间接成本的全成本口径。
- 模型成本来自调用发生时固化的 `llm_cost_usd_micro`，价格变更不回写历史。
- 所有金额均为 USD；未配置的假设按 0 计算，报告会将 `has_cost_assumptions` 标为 false。

## 记录一次结果

管理员在诊断、Incident 或变更结束后写入结果。`idempotency_key` 应来自工单或自动化事件；同键同载荷安全重放，同键不同载荷返回 409。

```bash
curl -X POST "$SRE_URL/billing/pilot-outcomes" \
  -H "X-SRE-API-Key: $SRE_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "idempotency_key": "INC-1042-final",
    "category": "incident",
    "incident_id": "<sre-incident-id>",
    "service_name": "payment-service",
    "baseline_minutes": 120,
    "actual_minutes": 35,
    "support_minutes": 15,
    "recommendation_accepted": true,
    "successful": true,
    "occurred_at": "2026-07-20T09:30:00Z"
  }'
```

`baseline_minutes` 是客户基线流程的人工时间，`actual_minutes` 是本次实际人工时间；两者均存在时才计入节省时间。负的净节省会被保留，避免只记录成功案例。`support_minutes` 是供应方交付与支持投入。`recommendation_accepted` 和 `successful` 可留空，留空不会进入对应分母。

## 每周导出

```bash
curl -OJ \
  -H "X-SRE-API-Key: $SRE_ADMIN_API_KEY" \
  "$SRE_URL/billing/value-report.csv?start_date=2026-07-13&end_date=2026-07-19"
```

也可使用 `/billing/value-report?month=2026-07` 获取 JSON，或通过 `/billing/pilot-outcomes` 抽查原始记录。时间统一按 UTC 计算，日期范围首尾均包含，最长 366 天。

报告中的客户劳动价值等于净节省小时乘客户时薪；客户净价值等于劳动价值减确认收入；交付成本等于折算基础设施成本、模型成本与支持成本之和；毛利等于确认收入减交付成本。连续四周保留 CSV、原始工单和客户负责人确认，再据此判断续费或扩容，不能用一次演示数据宣称投资回报。
