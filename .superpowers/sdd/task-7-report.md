# Task 7 Report — 绑定 ProjectContext 的项目壳层

## Status

完成。`/projects/[project_slug]/layout.tsx` 现在是项目身份的唯一前端 owner：挂载
`ProjectContextProvider`，按当前账户解析 slug，执行一次 enter mutation，并在 ready 前阻止
项目子树渲染。项目页和占位子页只消费 provider，不再重复解析或 enter。

## Implementation

- 新增 `ProjectContextProvider`、`useEnteredProjectBySlug(userId, slug)` 和仅允许在 provider
  内调用的 `useCurrentProject()`。
- 复用 M1 的 account + slug + UUID identity coordinator；账户、slug 或项目 UUID 改变后，
  旧结果立即失去可见资格，迟到 enter 响应不能提交为当前项目。
- loading、slug lookup 错误、enter 错误和重试统一归项目 context owner；成功后以 enter 响应
  作为当前项目，避免渲染 lookup 快照。
- 新增桌面 sticky sidebar 与移动 Sheet 导航。项目导航仅包含项目概览、成员与邀请、按服务端
  `project.lifecycle.manage` / `project.update` capability 显示的项目设置、返回工作空间和账户入口。
  未从 role 推导可见性，也未添加 Agent、Skill、MCP、私有工作或自动化入口。
- `/members` 与 `/settings` 仅提供明确占位路由，不发起 Task 8 治理请求；在同一 slug 的子路由
  间切换不会再次 enter。
- 项目主页继续展示 M1 隐私边界、共享资产占位与 hard-disabled private CTA；原 header 中重复的
  返回入口已移到项目导航，项目名称、slug、role 和描述语义保留。
- 更新 `frontend/AGENTS.md`，记录项目 layout/context 的单一 owner 和 capability 可见性规则。

## TDD Evidence

### Baseline

- `pnpm test run project-home-state project-components`：9/9 通过。
- `PORT=3107 PLAYWRIGHT_BASE_URL=http://127.0.0.1:3107 pnpm exec playwright test tests/e2e/projects.spec.ts --workers=1`：7/7 通过。

### RED

- `pnpm test run project-shell project-context`：失败；两个 test file 分别因
  `@/components/projects/project-context` 和 `project-shell` 不存在而失败。
- `PORT=3107 PLAYWRIGHT_BASE_URL=http://127.0.0.1:3107 pnpm exec playwright test tests/e2e/projects.spec.ts --grep "project workbench supports" --workers=1`：失败；真实项目路由找不到
  “项目概览”链接。

### GREEN / Final Gates

- `pnpm test run project-shell project-context project-home-state project-components`：4 files，
  13/13 通过。
- `PORT=3107 PLAYWRIGHT_BASE_URL=http://127.0.0.1:3107 pnpm exec playwright test tests/e2e/projects.spec.ts --workers=1`：7/7 通过；包含项目壳层入口、成员/设置占位子路由不重复 enter、移动导航、
  dark mobile、无水平溢出和 private CTA 禁用边界。
- `pnpm check`：通过（ESLint + TypeScript）。
- `pnpm format`：通过。
- `git diff --check`：通过。
- 按任务要求未运行全量 E2E。

说明：首次仅设置 `PLAYWRIGHT_BASE_URL=3107` 时，Playwright 的 `next start` 仍尝试监听
3000 并因端口占用退出；正式 RED/GREEN 和最终证据均同时设置 `PORT=3107`，使用独立端口。

## Self-review

- 单一 owner：slug lookup 和 enter 只存在于 `project-context.tsx`；`ProjectHomeLoader` 只调用
  `useCurrentProject()`；E2E 在 home → members → settings 后仍断言 enter path 只有一次。
- 身份隔离：M1 identity coordinator 的 account/slug/UUID token 校验仍生效；provider 在 identity
  不匹配时只返回 loading/error，不渲染旧项目。
- 权限来源：成员入口面向成功解析的 active member；设置入口只检查服务端 capabilities；测试覆盖
  role=admin 无能力时隐藏、role=viewer 有能力时显示。
- 范围：没有治理 API、mutation 或 Task 8 业务组件；没有未实现的项目菜单入口。
- 响应式：桌面 sidebar、移动 Sheet 与 `min-w-0` / `overflow-x-hidden` 边界成立；Playwright
  375px viewport 的 scroll width 断言通过。
- M1 兼容：隐私说明、共享资产占位、disabled private CTA 及无 Thread 请求断言继续通过。

## Concerns

- Playwright/Next build 仍输出仓库既有的 NFT trace、Node localstorage 和颜色环境警告；不影响
  build 或 7/7 E2E 结果。
- 成员、邀请和设置治理业务按计划留给 Task 8；当前两个子页是无请求占位。

## Review

提交前只读代码审查结论为 **Ready: Yes**；Critical、Important、Minor 均无发现。审查确认
context 单一 owner、stale identity 拒绝、capability 导航、Task 8 范围边界、响应式布局和 M1
private CTA 边界均符合 brief。

## Important Timing Fix — stable identity lookup refresh

### Status

已修复 review 指出的 Important 时序问题。`ProjectContextProvider` 的 enter effect 现在只随
account + slug + project UUID 组成的稳定 identity 和必要 mutation callbacks 变化；同一 identity
的 lookup 响应即使返回新对象或改变 `request_id`，也不会 cleanup 慢速 attempt 或重复 POST
`/enter`。真实 account、slug、project UUID 变化仍由 coordinator 启动新 attempt，旧 token 和旧
project result 均不能提交为当前项目。

### RED

- 新增真实页面行为 E2E：hold 首次 `/enter`，通过 reconnect 触发 active slug lookup refetch，
  并将 lookup `request_id` 改为 `request-pending-refresh`。修复前断言失败：预期 1 次 `/enter`，
  实际收到 2 次相同 UUID 的 POST。
- 测试同时在首次 enter 完成后再次刷新 lookup 为 `request-completed-refresh`，覆盖 completed
  attempt 不重启。
- 删除 `project-context.test.tsx` 中读取源码并匹配 hook 名称的字符串测试；回归不依赖实现源码文本。

### GREEN / Final Gates

- `pnpm test run project-context project-home-state project-shell`：3 files，8/8 通过；新增 account、
  slug、project UUID 逐项切换时产生 fresh token 且旧 token/result 被拒绝的行为覆盖。
- `PLAYWRIGHT_SKIP_WEB_SERVER=1 PLAYWRIGHT_BASE_URL=http://localhost:3107 pnpm exec playwright test tests/e2e/projects.spec.ts --grep "stable identity once" --workers=1`：1/1 通过。
- `PLAYWRIGHT_SKIP_WEB_SERVER=1 PLAYWRIGHT_BASE_URL=http://localhost:3107 pnpm exec playwright test tests/e2e/projects.spec.ts --workers=1`：8/8 通过。
- `pnpm check`：通过；`pnpm format`：通过。
- 按要求未运行全量 E2E；3107 dev server 在验证后已停止。

### Concerns

- Playwright 自带的 production webServer 冷构建超过 120 秒超时，因此最终 E2E 使用独立
  `pnpm dev --port 3107` 和 `PLAYWRIGHT_SKIP_WEB_SERVER=1`；真实 Chromium 行为断言均通过。
- 首次以 `127.0.0.1` 访问 dev server 被 Next.js dev-origin 保护阻止 hydration；正式 RED/GREEN
  与完整 projects E2E 均改用同源 `http://localhost:3107`。
