# Fluva Frontend

Fluva Frontend 是 project-first 架构的 Next.js Web UI。它通过 Nginx
同源访问 Gateway，不执行 Agent graph，也不从浏览器状态推导账户、项目或资产权限。

完整应用从仓库根目录启动：

```bash
make dev
```

统一入口为 <http://localhost:2026>。只有独立调试前端时才在本目录运行
`pnpm dev`（默认端口 `3000`）。

## 架构边界

- Gateway 是认证和授权权威；页面只使用服务端返回的 capability。
- 项目客户端和缓存按 `account UUID + project UUID` 隔离，URL slug 只用于导航。
- API 响应先通过 strict Zod schema，旧 scope 的请求和流不得更新新项目。
- Model、Skill、MCP、Channels 的 Secret write 使用直接请求，不进入 TanStack Query/Mutation cache。
- SSE 和历史序号保持 PostgreSQL BIGINT 的十进制字符串语义。
- `BUILD_MODE=static` 是无认证、无网络的本地演示边界。

## 路由族

| 路由                         | 用途                                                   |
| ---------------------------- | ------------------------------------------------------ |
| `/workspace`                 | 账户级多项目工作区                                     |
| `/projects/[project_slug]/*` | 项目会话、资产、Memory、Connections、Automation 和设置 |
| `/admin/*`                   | system admin 平台治理                                  |
| `/[lang]/docs/*`             | 静态产品文档                                           |

## 目录

```text
frontend/
├── src/
│   ├── app/          # App Router 页面和 layout
│   ├── build/        # production/static 构建边界
│   ├── components/   # 项目、Workspace、资产、Admin 和 UI 组件
│   ├── content/      # 产品文档
│   ├── core/         # API、Auth、Project、Thread、Stream 和 Asset 领域逻辑
│   ├── hooks/        # 可复用 React hooks
│   ├── lib/          # 通用工具
│   └── styles/       # Tailwind 入口和主题
├── tests/            # unit、dynamic/static/real-backend Playwright
├── public/           # 静态资源
└── package.json
```

## 开发命令

| 命令                                | 用途                      |
| ----------------------------------- | ------------------------- |
| `pnpm install`                      | 安装依赖                  |
| `pnpm dev`                          | 启动 Turbopack 开发服务器 |
| `pnpm check`                        | ESLint 和 TypeScript 检查 |
| `pnpm test`                         | Rstest 单元测试           |
| `pnpm test:e2e`                     | 动态模式 Playwright       |
| `pnpm test:e2e:static`              | 静态边界 Playwright       |
| `pnpm build:production`             | 生产构建                  |
| `pnpm build:static`                 | 静态演示构建              |
| `pnpm format` / `pnpm format:write` | 检查或写入 Prettier 格式  |

前端默认使用同源 `/api/*`。只有明确处理跨域、Cookie 和认证影响时才覆盖
`NEXT_PUBLIC_BACKEND_BASE_URL`。

更多约束见 [前端开发指南](./AGENTS.md)；项目级安装、数据库和全栈命令见
[根 README](../README.md)。
