# 月度账单快照

`/billing/usage` 是持续变化的用量视图；月度账单快照是关闭账期后冻结的开票底稿。快照保留套餐、订阅状态、各项用量、请求额度、超额请求、合同月费、超额单价、应收金额、模型成本、操作者和 SHA-256 校验值。

## 定价配置

```env
SRE_PLAN_PRICE_USD_MONTHLY=1000
SRE_REQUEST_OVERAGE_USD_PER_1000=5
```

金额统一使用 USD，内部以十进制定点计算并保留到六位小数。付费套餐的月费必须大于 0，否则预览和定稿都会失败；存在超额但单价为 0 时报告会带警告。当前版本不计算税费、折扣、按天折算或多币种，这些项目必须由合同和后续发票适配层处理。

## 月结流程

只有已经结束的 UTC 月份可以定稿。先预览：

```bash
curl -H "X-SRE-API-Key: $SRE_ADMIN_API_KEY" \
  "$SRE_URL/billing/statements/preview?month=2026-06"
```

核对 `requests_used`、`included_requests`、`overage_requests` 与金额后，使用财务任务的稳定幂等键定稿：

```bash
curl -X POST "$SRE_URL/billing/statements/finalize" \
  -H "X-SRE-API-Key: $SRE_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "month": "2026-06",
    "idempotency_key": "close-default-2026-06-v1"
  }'
```

同一幂等键重试返回原记录；同一月份使用另一个键也不会生成第二份账单；同一键用于其他月份返回 409。定稿后到达的迟到事件只会出现在动态预览中，不会修改已定稿快照。

## 校验与归档

```bash
curl -H "X-SRE-API-Key: $SRE_ADMIN_API_KEY" \
  "$SRE_URL/billing/statements/2026-06/verify"

curl -OJ -H "X-SRE-API-Key: $SRE_ADMIN_API_KEY" \
  "$SRE_URL/billing/statements/2026-06/export.csv"
```

归档 JSON、CSV、`payload_hash`、合同版本和客户确认。应用会对解析后的规范化 JSON 重算哈希，因此可发现单独修改快照载荷；拥有数据库完全权限的攻击者仍可同时重写载荷和哈希，所以正式财务留痕必须把哈希和导出文件保存到客户或财务侧不可变存储。

## 数据保留关系

生产默认 `SRE_REQUIRE_FINALIZED_BILLING_BEFORE_USAGE_PURGE=true`。保留任务发现截止时间之前存在未定稿 UTC 月份时会拒绝清理用量，并列出阻塞月份。账单快照本身不参与普通数据保留清理；销毁期限应按合同、税务和所在地区法规单独制定。

定稿使用当时数据库中的套餐/额度和当时环境中的合同价格。应在月末关账后及时定稿，并在变更套餐或价格前完成上一账期，否则历史合同价格需要由后续价格版本表或发票系统提供；当前版本不会猜测历史价格。
