# ActWeave Backend

ActWeave 后端由 FastAPI Gateway、独立 Worker 和可选独立 Scheduler 组成。Gateway 负责认证、项目授权、准入和查询；Worker 独占 Agent graph 执行；Scheduler 只负责 Automation 到期准入。业务数据和运行状态均持久化到 PostgreSQL。

## 目录

- `app/gateway/`：HTTP API、认证和 readiness。
- `app/private_work/`：项目 Thread、Run、File、Artifact、Memory 服务。
- `app/projects/`：项目、成员、资产、配额和审计。
- `app/automations/`：项目 Automation definition/occurrence。
- `packages/harness/deerflow/`：Agent harness、middleware、tools 与持久化模型。
- `tests/`：精简后的业务核心与真实 PostgreSQL 核心测试。

## 权威边界

所有私有业务操作都要求认证账户和不可变项目上下文。Thread、Run、File、Artifact、Memory、Connection、Automation、Skill 和 MCP 都没有全局或 owner-only 生产入口。Worker 只消费 Gateway 已固定的 project/owner/Agent/asset snapshot。

Memory 运行策略和文档章节模板均为 PostgreSQL system policy。平台管理员在
`/admin/settings/system` 的 Memory 页面维护；`config.yaml` 不提供同名权威或回退。
章节模板只在作用域首次创建文档时冻结，后续更新不改写已有文档和 Run 快照。

## 常用命令

```bash
uv sync
make gateway
make worker
make scheduler
make test
make lint
make format
```

完整应用从仓库根目录启动：

```bash
make dev
```

## API

当前业务 API 以认证账户和项目为作用域：

- `/api/v1/auth/*`：登录、账户和认证状态。
- `/api/projects/*`：项目、成员和项目资产。
- `/api/projects/{project_id}/private-work/*`：项目私有 Thread/Run/File/Artifact。
- `/api/projects/{project_id}/memory/*`：项目 Memory。
- `/api/projects/{project_id}/automations/*`：项目 Automation。
- `/api/admin/*`：仅 system admin 可访问的平台资产与运维接口。

详见 [API](docs/API.md)、[架构](docs/ARCHITECTURE.md) 和 [配置](docs/CONFIGURATION.md)。
