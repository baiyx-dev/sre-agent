# 每客户独立试用交付包

该流程用于为一个设计合作客户准备一套独立、尚未领取的免费试用配置。它不会创建云资源，也不会自动向客户发送消息；它负责生成唯一密钥、固定工作区标识、Compose 隔离名、非敏感清单和交付操作说明，避免不同客户复用同一实例或密钥。

## 1. 创建交付包

在仓库根目录执行：

```powershell
python scripts/prepare_trial_delivery.py create `
  --workspace-id acme-sre `
  --workspace-name "Acme SRE" `
  --upgrade-contact-url mailto:sales@example.com `
  --trial-days 14 `
  --http-port 8000
```

默认创建被 Git 忽略的 `.trial-deliveries/acme-sre/`。工作区 ID 只能包含小写字母、数字和内部连字符，最长 40 位；它同时参与 Compose 项目隔离。目录已经存在时命令直接失败，不提供覆盖参数，避免静默轮换已交付实例的恢复密钥或邀请令牌。

交付目录包含：

| 文件 | 敏感级别 | 用途 |
|---|---|---|
| `.env.trial.local` | 高敏感 | PostgreSQL、恢复管理员、激活和执行保护密钥，以及工作区配置 |
| `delivery.json` | 内部非密钥 | 交付 ID、客户工作区、Compose 项目名和密钥短指纹 |
| `README.md` | 内部操作说明 | 该客户实例的验证、启动、交接和停止命令 |

生成器不会在终端、`delivery.json` 或 README 中打印任何密钥。短指纹只用于确认两个操作人员持有的是同一份交付包，不能用于认证。

## 2. 启动前验证

每次复制、恢复或交接交付目录后执行：

```powershell
python scripts/prepare_trial_delivery.py verify `
  --delivery-dir .trial-deliveries/acme-sre
```

验证会检查必填键、密钥强度与唯一性、密钥指纹、工作区元数据、升级地址和 Compose 项目隔离名，并确认密钥没有泄漏到非敏感文件。输出只包含交付 ID、工作区 ID、项目名和密钥数量。

## 3. Compose 隔离边界

严格使用交付 README 中的 `--project-name sre-trial-<workspace-id>`。Compose 用项目名隔离容器、网络和 PostgreSQL 卷；省略或复用该值可能让两个客户共享状态。

停止时使用相同项目名和 env 文件执行 `down`，默认保留数据卷。只有客户退出已经确认、所需导出和保留义务已完成、且销毁目标再次核对后，才允许执行 `down --volumes`。

## 4. 客户与运营方各自持有什么

- 客户只接收 `SRE_TRIAL_ACTIVATION_TOKEN`，通过密码管理器或独立安全渠道传递。
- 运营方保留 PostgreSQL 密码、`SRE_ADMIN_API_KEY` 和 `EXECUTION_GUARD_TOKEN`。
- 不发送完整 `.env.trial.local`，不粘贴到工单、聊天或邮件。
- 客户领取后立即确认首把工作区 admin Key 已进入其密码管理器；该 Key 无法从数据库恢复。

## 5. Render 托管

每个客户创建独立 Blueprint、PostgreSQL、Web 和 Worker，并使用唯一服务/数据库名称。将 `SRE_WORKSPACE_ID`、`SRE_WORKSPACE_NAME`、恢复管理员 Key、激活令牌、升级联系地址和执行保护令牌填入该客户的 Render 配置。Render 通过 `DATABASE_URL` 管理数据库连接，不要把只供本地 Compose 使用的 `SRE_POSTGRES_PASSWORD` 填入 Render。

仓库根目录的 `render.yaml` 是拓扑模板，不是多客户控制平面。不能让多个客户共享同一 Blueprint 或数据库，也不能把默认 `SRE_WORKSPACE_ID=default` 当作正式交付标识。

## 6. 轮换与重新签发

未领取实例需要轮换时，创建新的输出目录并重新部署全新实例；不要覆盖旧目录。已领取实例的工作区 API Key 通过 API 单独轮换，数据库密码、Bootstrap Key、Guard Token 或激活令牌的轮换必须按变更流程执行并保留审计。旧目录在确认新实例可用、客户和合规保留要求满足后再安全销毁。
