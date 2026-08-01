# Module 14 Channels 移植验收证据

验收日期：2026-07-30

## 验收结论

Module 14 的本地 Channels 配置边界、项目 Connections API、系统 Provider health 和
GitHub webhook fail-closed 已完成真实浏览器与本地 HTTP 验收。

验收使用新建的隔离数据库：

```text
deerflow_test_m14_20260730_a71c9e
```

验收后已删除该数据库，并只读查询确认剩余数量为 `0`。原 `.env`、`config.yaml` 和业务
数据库均未为本次验收改写、补表或删除。

## 浏览器实际访问结果

1. 在 `http://localhost:2026/setup` 初始化隔离测试管理员；
2. 进入默认项目的 `/projects/default-project/connections`；
3. Gateway 日志确认页面实际请求精确 project UUID 的：
   - `GET /api/projects/{project_id}/connections` → `200`
   - `GET /api/projects/{project_id}/connections/providers` → `200`
4. 页面显示“Connections 功能尚未启用”；
5. `/admin/operations` 显示：
   - Database、Schema、Worker fleet、Stream、Quota、Audit：Ready；
   - DingTalk、Discord、Feishu、GitHub、Slack、Telegram、WeChat、WeCom：
     Unavailable；
6. 未设置 `GITHUB_WEBHOOK_SECRET`，也未设置
   `DEER_FLOW_ALLOW_UNVERIFIED_GITHUB_WEBHOOKS`。Gateway 启动日志确认 GitHub
   webhook router 未挂载，本地 `POST /api/webhooks/github` 返回 `404`。

## 截图

| 文件 | 证明内容 |
| --- | --- |
| [01-project-connections-disabled.png](01-project-connections-disabled.png) | 默认项目 Connections 的显式禁用态 |
| [02-admin-channel-providers-unavailable.png](02-admin-channel-providers-unavailable.png) | 核心服务 Ready 与 8 个 Provider 的 Unavailable 状态 |

截图不包含密码、Cookie、会话令牌、数据库连接串或 Provider/模型密钥。

## 自动化门禁

- Module 14 聚焦 Channels/GitHub/Provider：`534 passed, 41 skipped`
- durable admission PostgreSQL 聚焦用例：`3 passed, 0 skipped`
- 后端完整测试：`7714 passed, 1020 skipped, 0 failed`
- 固定 M1–M7 真实 PostgreSQL gate：`273 passed, 0 skipped`
- Ruff check：通过
- Ruff format check：`1146 files already formatted`
- `git diff --check`：通过
- canonical schema digest：
  `1192cc0d286f8195f91460b2571ad206a44f46cd4aa4e1bde5d4bebeff91df94`

## 未宣称通过的外部 Provider 环境

本地没有 GitHub、Feishu、WeCom、Slack、Telegram、Discord、DingTalk 或 WeChat 的真实
测试凭据。因此本次不宣称以下链路已通过：

- 外部平台签名 delivery；
- 外部平台 redelivery；
- Provider 入站触发真实模型；
- Provider outbound/reaction/working-card；
- 网络级 exactly-once outbound。

当首个 Run 已提交但 Gateway 在 outbound 前崩溃时，当前没有 outbox 补发；重投会被
durable delivery 去重，回复仍可能丢失。Provider publish 前的 reaction/working-card 也
仍可能重复。这些边界没有被浏览器截图掩盖。
