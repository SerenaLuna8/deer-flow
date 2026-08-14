# 后端文档索引

本目录记录当前实现、配置和运维边界。源码、OpenAPI 和当前测试是精确行为依据；
历史里程碑或旧版本说明不能替代当前 checkout 的验证。

## 架构与接口

| 文档                                   | 内容                                             |
| -------------------------------------- | ------------------------------------------------ |
| [ARCHITECTURE.md](./ARCHITECTURE.md)   | Gateway、Worker、Scheduler、harness 和持久化边界 |
| [API.md](./API.md)                     | project-scoped API 路由族与错误契约              |
| [STREAMING.md](./STREAMING.md)         | Run stream、durable SSE 和客户端事件             |
| [PATH_EXAMPLES.md](./PATH_EXAMPLES.md) | HTTP、Sandbox 和本地路径示例                     |
| [REPLAY_E2E.md](./REPLAY_E2E.md)       | 单场景真实 Gateway Replay E2E                    |
| [SKILL_BUILDER.md](./SKILL_BUILDER.md) | Worker 专用 Skill Builder Agent、工具与依赖边界  |

## 配置与集成

| 文档                                                     | 内容                                        |
| -------------------------------------------------------- | ------------------------------------------- |
| [CONFIGURATION.md](./CONFIGURATION.md)                   | `config.yaml`、环境变量、系统策略和 Sandbox |
| [SSO.md](./SSO.md)                                       | OIDC SSO 配置                               |
| [IM_CHANNEL_CONNECTIONS.md](./IM_CHANNEL_CONNECTIONS.md) | 项目 Channel Connection                     |
| [MCP_SERVER.md](./MCP_SERVER.md)                         | MCP、Credential 和 Run snapshot             |
| [APPLE_CONTAINER.md](./APPLE_CONTAINER.md)               | Apple Container Sandbox                     |

## Agent harness

| 文档                                                   | 内容                          |
| ------------------------------------------------------ | ----------------------------- |
| [GUARDRAILS.md](./GUARDRAILS.md)                       | Tool call 前置策略与 provider |
| [HOST_EXECUTION_APPROVAL.md](./HOST_EXECUTION_APPROVAL.md) | Local Provider 本机命令单次审批设计（未实现） |
| [FILE_UPLOAD.md](./FILE_UPLOAD.md)                     | 文件上传和转换                |
| [AUTO_TITLE_GENERATION.md](./AUTO_TITLE_GENERATION.md) | Thread title 生成             |
| [plan_mode_usage.md](./plan_mode_usage.md)             | Plan Mode 与 Todo middleware  |
| [summarization.md](./summarization.md)                 | 上下文压缩和 Memory 输入      |
| [TUI.md](./TUI.md)                                     | 本地 Terminal UI              |

## 诊断与性能

| 文档                                                         | 内容                       |
| ------------------------------------------------------------ | -------------------------- |
| [BLOCKING_IO_DETECTION.md](./BLOCKING_IO_DETECTION.md)       | Async 路径阻塞 IO 静态检查 |
| [SANDBOX_MEMORY_PROFILING.md](./SANDBOX_MEMORY_PROFILING.md) | Sandbox 内存基线流程       |

## 其他入口

- [项目快速开始](../../README.md)
- [安装流程](../../Install.md)
- [后端模块概览](../README.md)
- [后端开发约定](../AGENTS.md)
- Gateway 运行后：`/docs` 或 `/openapi.json`
