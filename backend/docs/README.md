# 后端文档索引

这里仅保留描述当前实现、配置和运维边界的文档。历史里程碑、一次性实现报告和已删除迁移不作为运行依据；精确行为以代码、OpenAPI 和当前测试门禁为准。

## 架构与接口

| 文档 | 内容 |
| --- | --- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Gateway、Worker、Scheduler、Agent harness 与持久化边界 |
| [API.md](./API.md) | 当前 project-scoped API 路由族和错误契约 |
| [PATH_EXAMPLES.md](./PATH_EXAMPLES.md) | HTTP、Sandbox 和本地配置路径示例 |
| [STREAMING.md](./STREAMING.md) | Run stream、durable SSE 与客户端事件处理 |
| [REPLAY_E2E.md](./REPLAY_E2E.md) | Gateway 录制与确定性 replay E2E |

## 配置与集成

| 文档 | 内容 |
| --- | --- |
| [CONFIGURATION.md](./CONFIGURATION.md) | `config.yaml`、环境变量和 Sandbox 配置 |
| [SSO.md](./SSO.md) | OIDC SSO 配置 |
| [IM_CHANNEL_CONNECTIONS.md](./IM_CHANNEL_CONNECTIONS.md) | Project-bound IM Channel Connection |
| [MCP_SERVER.md](./MCP_SERVER.md) | MCP 资产、Credential 和 Run snapshot |
| [APPLE_CONTAINER.md](./APPLE_CONTAINER.md) | Apple Container Sandbox 说明 |

## Agent harness

| 文档 | 内容 |
| --- | --- |
| [GUARDRAILS.md](./GUARDRAILS.md) | Tool call 前置授权与 provider |
| [FILE_UPLOAD.md](./FILE_UPLOAD.md) | 文件上传和转换 |
| [AUTO_TITLE_GENERATION.md](./AUTO_TITLE_GENERATION.md) | Thread title 生成 |
| [plan_mode_usage.md](./plan_mode_usage.md) | Plan Mode 与 Todo middleware |
| [summarization.md](./summarization.md) | 上下文压缩 |
| [TUI.md](./TUI.md) | 本地嵌入式 Terminal UI |

## 诊断与性能

| 文档 | 内容 |
| --- | --- |
| [BLOCKING_IO_DETECTION.md](./BLOCKING_IO_DETECTION.md) | Async/线程边界静态与动态检查 |
| [SANDBOX_MEMORY_PROFILING.md](./SANDBOX_MEMORY_PROFILING.md) | Sandbox 内存基线与分析流程 |

## 权威入口

- 项目安装和全栈命令：[`../../README.md`](../../README.md)
- 面向 Coding Agent 的安装流程：[`../../Install.md`](../../Install.md)
- Project-first SaaS 设计：[`../../docs/2026-07-12-project-first-saas-design.md`](../../docs/2026-07-12-project-first-saas-design.md)
- 后端代码约定和模块地图：[`../AGENTS.md`](../AGENTS.md)
- 精确请求/响应模型：启动 Gateway 后访问 `/docs` 或 `/openapi.json`
