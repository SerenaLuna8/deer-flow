# Task 6 报告：登录后工作空间和旧版壳层分流

## 范围

- 基线：`757e966e`
- 分支：`codex/project-governance-m2`
- 仅实现 M2 Task 6；未实现项目壳层、成员治理或邀请 UI。

## 实现

- 非静态 `/workspace` 直接渲染多项目工作空间。
- `/workspace/projects` 使用服务端 `redirect("/workspace")`。
- 新增 `WorkspaceRouteFrame`：`/workspace` 与兼容地址不挂载旧壳层，其余
  `/workspace/chats`、agents、memory、skills、tools、scheduled-tasks 路由继续使用
  既有 `WorkspaceContent`。
- `WorkspaceContent` 依赖服务端 `cookies()`，因此由 server layout 生成旧壳层
  ReactNode，再交给 client frame 按 pathname 选择，避免 Client Component 直接导入
  async Server Component。
- 工作空间头部只保留 DeerFlow 标识、“工作空间”和账户菜单；账户菜单展示当前邮箱并
  提供退出登录。项目创建、搜索、置顶、编辑和进入行为保持 M1 contract。
- 静态 demo 的原有 chat landing 分支保留；非静态 landing helper 改为 `/workspace`。
- 直接工作空间在 Gateway 离线且用户尚未恢复时继续显示既有恢复 banner，避免空白页。
- Rstest 默认发现范围扩展到 `.test.tsx`，确保 brief 指定的 route-frame 单测实际进入门禁。
- 同步更新 `frontend/AGENTS.md` 与 `README_zh.md` 的默认工作空间路由说明。

## TDD 记录

### RED

1. focused unit：
   `pnpm test run tests/unit/core/projects/project-first-mode.test.ts tests/unit/components/workspace/workspace-route-frame.test.tsx`
   - 7 个测试中 3 个按预期失败：非静态 landing 仍返回 `/workspace/projects`、
     `WorkspaceRouteFrame` 不存在、`/workspace` 尚未直接渲染 `ProjectWorkbenchPage`。
2. focused E2E，独立端口 3106：
   `PORT=3106 PLAYWRIGHT_BASE_URL=http://127.0.0.1:3106 pnpm exec playwright test tests/e2e/projects.spec.ts --grep "workspace shows project cards without project navigation" --workers=1`
   - 按预期失败：`Agents` 链接仍有 1 个，证明工作空间仍处于旧侧栏。
3. 自审补充的离线恢复回归先 RED：
   `pnpm test run tests/unit/core/projects/project-first-mode.test.ts -t "gateway-offline recovery"`
   - 按预期失败：直接工作空间的 null-user 分支尚未渲染恢复 banner。

### GREEN

- focused unit：2 files，8 tests，全部通过。
- focused workspace E2E：2 tests，全部通过。
- required projects/sidebar E2E：11 tests，全部通过，`workers=1`，独立端口 3106。
- `pnpm check`：通过（ESLint + TypeScript）。
- `pnpm format`：通过。
- `git diff --check`：通过。

## 自审

- `/workspace` 无 Agents、Skills、Tools、Memory、Scheduled tasks 链接，并保留账户入口。
- `/workspace/projects` 最终 URL 为 `/workspace`。
- `/workspace/chats/new` 仍显示既有 sidebar，且未渲染 project workbench。
- 现有项目 create/search/pin/edit/enter/error/retry 与 mobile/private-work E2E 均通过。
- 路由 frame 未引入 Task 7 的项目 shell 或治理导航。
- 未暂存共享 worktree 中其他任务的报告、审查 diff 或 progress 文件。

## 顾虑

- Playwright 的 Next build 继续输出既有 NFT 动态文件跟踪警告，以及 Node/颜色环境警告；
  本任务构建和全部目标断言仍通过，未发现 Task 6 引入的新构建错误。
