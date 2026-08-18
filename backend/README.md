# ActWeave Backend

ActWeave 后端由 FastAPI Gateway、独立 Worker 和可选 Scheduler 组成。
Gateway 负责认证、项目授权、Run 准入和查询；Worker 独占 Agent graph 与持久化
Job 执行；Scheduler 只负责 Automation 到期准入。

PostgreSQL 是业务元数据、运行状态、资产版本、Checkpoint、Job、流和审计的权威。
文件与 Artifact 字节可位于配置的存储后端，但其身份、版本和访问范围仍由数据库控制。

## 目录

| 路径                         | 内容                                                      |
| ---------------------------- | --------------------------------------------------------- |
| `app/gateway/`               | HTTP API、认证、readiness 和 server-issued context        |
| `app/worker/`                | Agent graph、Job、lease 和终态结算                        |
| `app/scheduler/`             | Automation 轮询和准入                                     |
| `app/private_work/`          | Thread、Run、File、Artifact、Memory 等 owner-private 服务 |
| `app/shared_assets/`         | Agent、Skill、MCP、Credential 治理                        |
| `packages/harness/deerflow/` | Agent harness、middleware、tools、sandbox 和持久化        |
| `migrations/`                | 存量数据库的显式 Alembic 迁移链                           |
| `tests/`                     | 单元、PostgreSQL、进程和契约测试                          |

依赖方向固定为 `app.* -> deerflow.*`；harness 不得导入应用层。

## 核心边界

- 私有业务操作必须使用认证账户、服务端签发的项目上下文和 owner scope。
- Gateway 只准入和读取；Worker 是唯一 Agent graph 执行者。
- Run 固定精确的 Agent、模型、Skill、MCP 和 Credential 引用快照。
- System 资产定义由 packaged catalog 初始化；项目资产使用不可变版本。
- Credential 明文只在 Worker 的精确执行边界解密，不进入 API、日志或快照。
- `full_schema.sql` 用于新空库并直接记录当前链头 revision `agent_design_resume_index`；运行时
  从不自动创建、迁移或修复 schema。

## 开发命令

在 `backend/` 运行：

| 命令                                              | 用途                           |
| ------------------------------------------------- | ------------------------------ |
| `uv sync`                                         | 安装后端依赖                   |
| `make gateway` / `make worker` / `make scheduler` | 启动单个进程                   |
| `make test`                                       | 使用随机隔离数据库运行核心测试 |
| `make lint` / `make format`                       | Ruff 检查与格式化              |
| `make check-db`                                   | 只读检查 schema readiness      |

完整应用从仓库根目录运行 `make dev`。数据库初始化和升级也应从根目录按
[安装流程](../Install.md)执行。

## 文档

- [后端文档索引](docs/README.md)
- [架构](docs/ARCHITECTURE.md)
- [API](docs/API.md)
- [配置](docs/CONFIGURATION.md)
- [开发约定](AGENTS.md)

精确请求/响应模型以运行中的 `/openapi.json`、源码和当前测试为准。
