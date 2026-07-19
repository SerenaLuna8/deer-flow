# 贡献指南

感谢参与 DeerFlow。当前主线是项目优先、多账户、PostgreSQL-only 的完整应用；变更必须保持 Gateway、Worker、Scheduler、Frontend 和数据库契约一致。

## 开发环境

```bash
make check
make config
make install
make setup-db
make dev
```

浏览器统一入口是 `http://localhost:2026`。nginx 将 `/api/*` 转发给 Gateway，非 API 请求交给 Frontend。Agent graph 由独立 Worker 执行。

## 仓库结构

```text
backend/                  FastAPI Gateway、Worker、Scheduler、harness
frontend/                 Next.js 项目工作区
contracts/                跨组件 JSON contract
docker/                   compose、nginx、provisioner
deploy/                   Helm 等部署资产
skills/public/            内置公开 Skill bytes
tests/                    根级门禁
docs/                     当前运维文档和历史设计归档
```

项目 Agent、Skill、MCP 和 Credential 是 PostgreSQL 版本化资产。不要添加本地全局资产清单或私有资源文件 fallback。

## 分支与提交

1. 从当前开发分支创建短生命周期 feature branch。
2. 先写失败测试，再实现最小修复。
3. 更新用户文档和相应 `AGENTS.md`。
4. 格式化并运行与风险匹配的门禁。
5. 提交只包含本任务变更。

## 后端

```bash
cd backend
make format
make lint
make test
```

后端业务 repository 必须接受不可变 project/owner authority，并把 scope 写进 SQL predicate。禁止依赖默认用户、owner-only 查询、全局 checkpointer/store 或本地文件私有数据。

## 前端

```bash
cd frontend
pnpm check
pnpm test
pnpm build
```

项目私有 URL 和 query key 必须同时绑定 account 与 project。服务端响应用严格 schema 解析，未知字段和已退役错误码必须拒绝。

## PostgreSQL 发布门禁

从根目录运行：

```bash
POSTGRES_TEST_URL=postgresql+asyncpg://... make test-project-foundation-postgres
```

固定文件清单只由根 `Makefile` 的 `PROJECT_FOUNDATION_POSTGRES_TESTS` 定义。门禁必须 0 skip，并且只能创建随机测试数据库。

## 配置变更

配置 schema 位于 `backend/packages/harness/deerflow/config/`。新增字段时同步更新 `config.example.yaml`、配置文档、严格解析测试和部署模板。已删除的 legacy key 只能保留在 tombstone validator 中。

## MCP、Skill 与 Agent

平台资产通过 system admin API 发布，项目资产通过项目设置创建并绑定不可变版本。Credential secret 使用 imperative authenticated API 和加密 envelope；不得进入定义、日志、query cache 或测试快照。

## 提交前检查

```bash
make doctor
make check-db
make support-bundle
```

不要提交 `.env`、`config.yaml`、数据库凭据、support bundle 或运行时输出。
