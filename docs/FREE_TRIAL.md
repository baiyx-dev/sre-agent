# 免费试用开通与转化闭环

当前免费试用采用“每个试用客户一个独立部署、一个工作区”的隔离方式，不允许多个互不信任客户共享实例。收费套餐、订阅门禁、用量计量和账单底稿已经保留，但真实支付与自动开票后置；现阶段通过人工联系完成升级。

## 两种试用起始方式

- `SRE_TRIAL_START_MODE=deployment`：数据库首次初始化即开始计时，兼容已有部署。
- `SRE_TRIAL_START_MODE=activation`：新工作区保持 `pending_activation`，用户领取后才开始计时，适合邀请制免费试用。

自助领取需要：

```env
SRE_PLAN=trial
SRE_TRIAL_DAYS=14
SRE_TRIAL_START_MODE=activation
SRE_TRIAL_SELF_SERVICE_ENABLED=true
SRE_TRIAL_ACTIVATION_TOKEN=<至少 32 位的唯一随机值>
SRE_UPGRADE_CONTACT_URL=mailto:sales@example.com
```

每个客户部署必须使用不同的激活令牌，并通过 Render Secret、Kubernetes Secret、Vault 或同等设施注入。令牌错误会按请求来源和实例总量限速；数据库只保存来源摘要、令牌短指纹和成功状态，不保存令牌明文。

## 用户领取流程

1. 打开首页，前端调用公开的 `GET /trial/status`。
2. 如果实例尚未领取，填写邀请令牌、工作区名称、管理员姓名和联系邮箱。
3. `POST /trial/activate` 在一个数据库事务中启动试用期限、追加订阅事件、登记联系人并签发首把 admin 工作区 API Key。
4. 明文 API Key 只在该响应出现一次；前端保存在当前标签页的 `sessionStorage`，用户应立即复制到密码管理器。
5. 网络中断导致密钥丢失时，部署管理员使用环境 Bootstrap Admin Key 创建新的工作区密钥；不能从数据库恢复旧密钥。

领取接口是一次性的。已经领取、并发领取或非 trial 工作区返回 409。试用到期后业务接口返回 402，但身份、工作区、账单和 `/trial/*` 仍可访问，用户可以导出数据并提交最终反馈。

自助试用的 trial 套餐在接入真实数据源前允许 readiness 通过，使托管平台能够承载领取和 onboarding；`details.trial_data_source_onboarding_grace=true` 会明确暴露这个状态。该宽限只适用于启用了自助领取的 trial，升级为付费套餐后 `SRE_REQUIRE_REAL_DATA_SOURCE=true` 仍会严格阻止空数据源实例通过 readiness。

## 首次价值路径

`GET /trial/onboarding` 返回五个可复算里程碑：

1. 激活免费试用。
2. 配置一个监测目标。
3. 完成首次有数据证据的状态查询。
4. 完成首次有数据证据的故障诊断；该时间作为 first value。
5. 提交试用反馈。

响应同时给出完成比例、下一步、首次价值时间、诊断证据来源数和从激活到首次价值的分钟数。仅发起查询、进入澄清流程或对不存在的服务执行无证据排障不会完成里程碑。前端直接展示该清单，不依赖浏览器本地标记来伪造完成状态。

## 部署后一次性验收

对尚未交付、允许被永久领取的全新测试实例执行：

```powershell
$env:SRE_TRIAL_ACTIVATION_TOKEN="<该测试实例的激活令牌>"
python scripts/trial_activation_smoke.py --base-url https://trial.example.com --confirm-disposable-instance
```

该脚本会真实领取实例，验证领取前后 readiness、公开状态、匿名拒绝、一次性 admin Key、重复领取冲突、反馈幂等和转化指标，因此绝不能对已经分配给客户或需要保留未领取状态的实例运行。脚本不会打印激活令牌或签发的 API Key。常规已领取实例继续使用 `scripts/smoke_test.py` 验收。

## 反馈与升级

`POST /trial/feedback` 记录 1–5 分评分、价值结果、付费意向、缺失能力、备注和联系授权。请求必须带幂等键；相同载荷重放不重复写入，同一键修改载荷返回 409。自由文本可能包含个人或客户信息，应按隐私政策处理。

管理员通过 `GET /trial/conversion-metrics` 查看激活信息、onboarding、首次价值时间、平均评分、高价值反馈和付费意向。只有 `contact_consent=true` 的反馈才可用于主动产品或升级沟通。

`SRE_UPGRADE_CONTACT_URL` 只接受 `https://` 或 `mailto:`。当前返回 `payment_automation=false`，明确表示升级由人工完成；不得向用户宣称已经支持在线支付。

## 运营指标

每周至少复核：

- 邀请实例数、领取率和领取失败率。
- 接入目标率、首次查询率、首次诊断率。
- 中位数与 P95 time-to-first-value。
- 反馈完成率、平均评分、高价值占比。
- `yes`/`maybe` 付费意向和已授权联系人数。
- 每个激活试用的推理、托管和支持成本。

激活失败记录默认保留 30 天，反馈默认保留 730 天；分别使用 `SRE_RETENTION_TRIAL_ATTEMPT_DAYS` 和 `SRE_RETENTION_TRIAL_FEEDBACK_DAYS` 调整。试用激活记录是订阅与密钥审计依据，不随普通保留任务删除。
