# MCP Server（项目资产）

MCP 在 M7 中是版本化 PostgreSQL 资产，不再从本地扩展清单加载。

## 管理模型

- system admin 在 `/admin/assets/mcp` 发布 system MCP definition/version。
- 项目成员在 `/projects/{project_slug}/mcp` 创建 project MCP，或绑定允许的 system version。
- Credential 通过独立加密 envelope 和 grant 管理；secret 不写入 definition，也不进入浏览器 cache。
- Gateway admission 为 Run 固定 exact MCP version 和 Credential grant snapshot。
- Worker 只 dispatch admitted snapshot，实际调用在 quota 和 audit 事务边界记账。

## 权限

Viewer 只能读取 capability 允许的项目 binding；创建、更新、approve 和 secret replace 要求对应管理 capability。普通用户不能访问平台资产页面。

## 运维

轮换 Credential 使用 `make rotate-credentials ARGS="--dry-run --key-id <id>"`。目标 key 必须已激活，批处理采用 gap-safe 锁定并保留审计 checkpoint。
