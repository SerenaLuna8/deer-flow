# GitHub Copilot Instructions

本仓库的 Agent 指令唯一权威是根目录 [`AGENTS.md`](../AGENTS.md)。处理文件前先读取它；进入模块后再读取对应模块说明：

- 后端：[`backend/AGENTS.md`](../backend/AGENTS.md)
- 前端：[`frontend/AGENTS.md`](../frontend/AGENTS.md)

不要在本文件复制端口、测试数量、目录清单或运行架构，避免与当前代码和模块指令漂移。

最低工作约定：

- 保留 PostgreSQL-only、project-first、Gateway admission / Worker execution 边界。
- 私有资源必须同时受 account、project、membership 和 owner context 约束；不使用 PostgreSQL RLS。
- 行为变更必须配套测试；文档和配置变更必须同步当前入口文档。
- 后端使用 `cd backend && make lint`，完整核心测试使用 `make test`；前端使用 `cd frontend && pnpm check && pnpm test`。
- 核心套件从开发环境 `DATABASE_URL` 派生随机 `deerflow_test_*` 数据库，绝不改动命名开发库，并保持 0 skip。
- 不提交 `config.yaml`、`.env`、Credential、数据库连接信息或运行期私有数据。

根级全栈命令和当前目录结构见 [`README.md`](../README.md) 与 `make help`。
