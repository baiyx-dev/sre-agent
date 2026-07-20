# 本地生产模式免费试用部署

该方案用于在一台安装了 Docker Desktop（Windows/macOS）或 Docker Engine + Compose v2（Linux）的机器上运行 PostgreSQL、Web 和耐久 Worker。它使用生产安全门禁，但只绑定 `127.0.0.1`，适合交付前演练、内网试用或通过受控反向代理发布，不应直接把 8000 端口暴露到公网。

## 1. 生成本机密钥

只做单机演练时可以使用下面的简化生成器。为真实客户准备独立实例时，必须改用[每客户独立试用交付包](TRIAL_DELIVERY.md)，由它固定工作区 ID、Compose 项目名、密钥指纹和防覆盖边界。

```powershell
python scripts/generate_trial_env.py `
  --upgrade-contact-url mailto:sales@example.com
```

命令创建被 Git 忽略的 `.env.trial.local`，其中包含随机 PostgreSQL 密码、恢复管理员 Key、试用激活令牌和执行保护令牌。明文不会打印到终端。默认拒绝覆盖已有文件；只有明确执行 `--force` 才会轮换全部值。已领取或已交付实例不要随意轮换。

检查 Compose 能否解析，但不要输出包含密钥的完整配置：

```powershell
docker compose `
  --env-file .env.trial.local `
  -f compose.yaml `
  -f compose.trial.yaml `
  config --quiet
```

## 2. 启动完整栈

```powershell
docker compose `
  --env-file .env.trial.local `
  -f compose.yaml `
  -f compose.trial.yaml `
  up --build --detach --wait --wait-timeout 180
```

打开 `http://127.0.0.1:8000`。领取前 `/health/ready` 也必须返回 200；它会验证 PostgreSQL、Worker 心跳、激活令牌、恢复管理员 Key、升级联系地址和其他生产门禁。

## 3. 一次性交付验收

以下操作会永久领取该实例，只能用于准备销毁的验收实例，不能对即将交给客户的未领取实例运行：

```powershell
python scripts/trial_activation_smoke.py `
  --base-url http://127.0.0.1:8000 `
  --activation-token-file .env.trial.local `
  --confirm-disposable-instance
```

脚本验证领取前后 readiness、匿名拒绝、一次性管理员 Key、重复领取冲突、反馈幂等和转化指标。CI 会在全新的容器卷上执行同一流程，因此仓库中的 Compose 路径不是只做语法检查。

真实交付时不要运行验收脚本；通过密码管理器或其他安全渠道把 `SRE_TRIAL_ACTIVATION_TOKEN` 单独发送给客户。恢复管理员 Key、数据库密码和执行保护令牌只由部署方保管。

## 4. 停止与再次启动

停止容器但保留 PostgreSQL 卷：

```powershell
docker compose `
  --env-file .env.trial.local `
  -f compose.yaml `
  -f compose.trial.yaml `
  down
```

再次运行 `up --build --detach --wait` 会复用原卷，试用截止时间不会重置。不要添加 `--volumes`，除非明确要永久删除整个试用实例。正式客户数据还应按 [运维手册](OPERATIONS_RUNBOOK.md) 执行备份、恢复和审计链归档。

## 5. 当前商业边界

本地生产模式包含试用额度、订阅状态、用量/成本、价值报告和不可变月账单，但不会自动扣款或开票。客户决定升级后，部署方通过 `SRE_UPGRADE_CONTACT_URL` 沟通合同，再按 [订阅生命周期](SUBSCRIPTION_LIFECYCLE.md) 更新为付费套餐。
