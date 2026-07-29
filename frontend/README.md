# DeerFlow Frontend

DeerFlow Frontend 是 project-first SaaS 的 Next.js Web UI。它通过 Nginx 同源访问 Gateway，不直接执行 Agent graph，也不把账户、项目、成员或资产权限建立在浏览器状态上。

完整应用请从仓库根目录启动：

```bash
make dev
```

统一访问入口是 <http://localhost:2026>。单独开发前端时才在本目录运行 `pnpm dev`，默认端口为 `3000`。

## 技术栈

- Next.js 16 App Router
- React 19
- TypeScript
- Tailwind CSS 4
- TanStack Query
- Rstest 与 Playwright

## 主要路由

| 路由                                   | 用途                  |
| -------------------------------------- | --------------------- |
| `/login`、`/setup`                     | 登录和首次账户初始化  |
| `/workspace`                           | 多项目工作区          |
| `/projects/[project_slug]`             | 项目主页              |
| `/projects/[project_slug]/chats/*`     | 项目 Thread 与 Run    |
| `/projects/[project_slug]/agents`      | 项目 Agent            |
| `/projects/[project_slug]/skills`      | 项目 Skill            |
| `/projects/[project_slug]/mcp`         | 项目 MCP              |
| `/projects/[project_slug]/credentials` | 项目 Credential       |
| `/projects/[project_slug]/automations` | 项目 Automation       |
| `/projects/[project_slug]/memory`      | 项目 Memory           |
| `/projects/[project_slug]/members`     | 项目成员              |
| `/admin/*`                             | system admin 平台治理 |
| `/[lang]/docs/*`、`/blog/*`            | 静态文档与博客内容    |

## 目录

```text
frontend/
├── src/
│   ├── app/                 # App Router 页面、layout 和 route handler
│   ├── components/          # UI、Workspace 与 AI 组件
│   ├── core/                # API、Thread、Stream、Asset 等领域逻辑
│   ├── hooks/               # React hooks
│   ├── lib/                 # 通用库与工具
│   ├── server/              # 服务端认证和代理逻辑
│   └── styles/              # 全局样式
├── tests/
│   ├── unit/                # Rstest 单元测试
│   ├── e2e/                 # 确定性动态模式 Playwright E2E
│   ├── e2e-static/          # 静态构建 Playwright E2E
│   ├── e2e-real-backend/    # Replay Gateway 全栈回放
│   └── e2e-record/          # 人工录制 Replay fixture
├── public/                  # 静态资源和确定性演示 fixture
├── scripts/                 # 前端辅助脚本
└── package.json
```

更完整的数据流、路由权限和代码约定见 [`AGENTS.md`](./AGENTS.md)。

## 开发命令

```bash
pnpm install
pnpm dev
pnpm check
pnpm test
```

| 命令                    | 用途                           |
| ----------------------- | ------------------------------ |
| `pnpm dev`              | 使用 Turbopack 启动开发服务器  |
| `pnpm check`            | 运行 ESLint 和 TypeScript 检查 |
| `pnpm test`             | 运行 Rstest 单元测试           |
| `pnpm test:e2e`         | 运行动态模式 Playwright E2E    |
| `pnpm test:e2e:static`  | 运行静态模式 Playwright E2E    |
| `pnpm build`            | 构建标准版本                   |
| `pnpm build:production` | 构建生产模式版本               |
| `pnpm build:static`     | 构建静态模式版本               |
| `pnpm format`           | 检查 Prettier 格式             |
| `pnpm format:write`     | 写入 Prettier 格式             |

前端 API 默认走 Nginx/Next.js 同源代理。只有在独立调试且明确了解跨域和认证影响时，才覆盖 `NEXT_PUBLIC_BACKEND_BASE_URL` 或 `NEXT_PUBLIC_LANGGRAPH_BASE_URL`。

项目会话中的实时文件写入会自动打开右侧文件预览并收起桌面项目菜单；Agent 通过
`present_files` 明确交付的文件会在对话中显示可下载文件卡片。

## 权限与敏感数据

- 页面路由只是体验层，Gateway 才是认证和授权权威。
- 项目请求必须携带服务端解析的项目上下文；不要从 URL slug 或客户端 cache 推导 authority。
- Credential create/replace 等含 secret 的输入不得进入 TanStack Query/Mutation cache。
- system admin 页面由 server layout 与 Gateway 双重限制，普通用户不可发现。

项目级启动、数据库初始化和完整命令见根 [`README.md`](../README.md)。
