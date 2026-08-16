# Backend API（当前项目作用域）

Gateway 只公开认证账户、项目和平台管理 API。除公开认证入口与 provider-signed webhook 外，请求必须先通过 AuthMiddleware；项目路由随后解析不可变 ProjectContext 并检查 capability。

## 主要路由族

| 路由族 | 作用域 | 说明 |
| --- | --- | --- |
| `/api/v1/auth/*` | account | 登录、登出、账户和 OAuth |
| `/api/projects` | account | 项目列表与创建 |
| `/api/projects/{project_id}` | project | 项目详情、成员、readiness 和资产 binding |
| `/api/projects/{project_id}/private-work/threads` | project + owner | Thread 搜索、创建、更新、删除；删除使用确认版本 CAS，并在 tombstone 前强制终结该 Thread 的 Run/Job/Attempt、审批与 durable stream |
| `/api/projects/{project_id}/private-work/threads/{thread_id}/runs` | project + owner | private Run admission、stream、feed；feedback 使用 GET/PUT/DELETE，POST 仅保留弃用兼容 |
| `/api/projects/{project_id}/private-work/threads/{thread_id}/execution-approvals/active` and `/{approval_id}` | project + owner | Local host command active/by-id 状态；terminal 请求通过 by-id 恢复 |
| `/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{source_run_id}/execution-approvals/{approval_id}/decision` | project + owner | `allow_once` / `deny` CAS 决策；命令 authority 始终来自服务端冻结计划 |
| `/api/projects/{project_id}/private-work/threads/{thread_id}/uploads` | project + owner | upload、list 和 delete；Viewer 可删除自己的 ready upload/workspace/output，但不能创建或转换文件 |
| `/api/projects/{project_id}/private-work/artifacts/{artifact_id}` | project + owner | Run artifact 下载 |
| `/api/projects/{project_id}/memory` | project + owner | Memory 查询、导入、更新和清理 |
| `/api/projects/{project_id}/connections` | project + owner | IM connection/OAuth/inbound execution |
| `/api/projects/{project_id}/automations` | project + owner | Automation definition、occurrence 和手动触发 |
| `/api/projects/{project_id}/usage` | project | 配额策略与用量 |
| `/api/projects/{project_id}/audit` | project | 脱敏审计查询 |
| `/api/admin/*` | system admin | 平台资产、readiness、job 和审计运维 |

## 错误契约

项目未就绪统一返回当前 `*_UNAVAILABLE` 错误。配额拒绝返回稳定 429 错误和 `Retry-After`。客户端必须严格解析公开字段；未知或已退役的生命周期错误码视为无效响应。

## Run 与 stream

Gateway 在单个事务内持久化 Run、job、quota 和 admitted snapshot。Worker 写入 PostgreSQL durable stream；Gateway 读取 cursor 并支持 SSE 重连。cursor 与数据始终绑定 account/project/thread，不能跨作用域复用。

## OpenAPI

运行 Gateway 后访问 `/docs` 或 `/openapi.json` 查看精确请求/响应模型。部署时 nginx 仅把 `/api/*` 转发给 Gateway。
