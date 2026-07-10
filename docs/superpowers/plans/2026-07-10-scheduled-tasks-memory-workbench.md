# Scheduled Tasks and Memory Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将定时任务和记忆页面重排为宽屏、主从分栏的专注工作台，同时保持全部业务逻辑和后端契约不变。

**Architecture:** 页面级 controller 继续持有现有 hooks、状态、派生数据和 mutations；本次只改 JSX 组织、响应式布局和既有 UI 组件的使用。记忆页通过宽度变体获得约 1440px 内容区，定时任务创建表单迁入受控的 Radix Sheet，提交与 payload 仍由原页面代码负责。

**Tech Stack:** Next.js 16 App Router、React 19、TypeScript 5.8、Tailwind CSS 4、现有 shadcn/Radix 组件、Rstest、Playwright、pnpm。

## Global Constraints

- 只修改前端结构、组件组织和视觉样式，不修改后端接口、数据模型、权限、调度或记忆业务逻辑。
- 两页桌面端最大内容宽度约 `1440px`，主从区域约为 `36% / 64%`，小屏退化为单列。
- 定时任务创建表单使用右侧 `Sheet`，现有字段、配方、校验、payload、成功清理和错误状态全部保留。
- `ScheduledTasksPage` 继续拥有全部 hooks、状态、派生数据和 mutations。
- `MemorySettingsPage` 继续拥有全部 hooks、过滤状态、导入导出和 mutations。
- 记忆“全部”显示摘要和事实，“事实”仅显示全宽事实，“摘要”仅显示全宽摘要。
- 保留现有 `data-testid`、可访问名称、确认弹窗和深浅主题兼容性。
- 不新增第三方依赖，不重设计工具、技能或其他页面。
- 所有行为变更先写失败测试，再写生产代码。

---

### Task 1: 记忆宽工作台与面板切换

**Files:**
- Create: `frontend/tests/e2e/memory-workbench.spec.ts`
- Modify: `frontend/src/components/workspace/workspace-capability-page.tsx`
- Modify: `frontend/src/app/workspace/memory/page.tsx`
- Modify: `frontend/src/components/workspace/settings/memory-settings-page.tsx`

**Interfaces:**
- Produces: `WorkspaceCapabilityPage` 新增可选属性 `width?: "default" | "wide"`，默认值为 `"default"`。
- Produces: `data-testid="memory-workbench"`、`memory-summary-panel`、`memory-facts-panel`、`memory-filter-all`、`memory-filter-facts`、`memory-filter-summaries`。
- Consumes: 现有 `MemoryViewFilter`、`showSummaries`、`showFacts`、`filteredSectionGroups`、`filteredFacts` 和全部现有 mutations。

- [ ] **Step 1: 写记忆工作台失败 E2E**

创建 `frontend/tests/e2e/memory-workbench.spec.ts`：

```ts
import { expect, test, type Page } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

const memoryFixture = {
  version: "1",
  lastUpdated: "2026-07-10T00:00:00Z",
  user: {
    workContext: { summary: "Prefers source-backed work.", updatedAt: "2026-07-10T00:00:00Z" },
    personalContext: { summary: "Uses Chinese.", updatedAt: "2026-07-10T00:00:00Z" },
    topOfMind: { summary: "Redesigning DeerFlow.", updatedAt: "2026-07-10T00:00:00Z" },
  },
  history: {
    recentMonths: { summary: "Forked DeerFlow.", updatedAt: "2026-07-10T00:00:00Z" },
    earlierContext: { summary: "", updatedAt: "" },
    longTermBackground: { summary: "", updatedAt: "" },
  },
  facts: [
    {
      id: "fact-1",
      content: "Prefers Chinese responses",
      category: "preference",
      confidence: 0.95,
      createdAt: "2026-07-10T00:00:00Z",
      source: "manual",
    },
  ],
};

function mockMemoryAPI(page: Page) {
  void page.route("**/api/memory", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(memoryFixture),
    }),
  );
}

test("memory workbench switches between summary and facts panels", async ({ page }) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await expect(page.getByTestId("memory-workbench")).toBeVisible();
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
  await expect(page.getByTestId("memory-facts-panel")).toBeVisible();

  await page.getByTestId("memory-filter-facts").click();
  await expect(page.getByTestId("memory-summary-panel")).toHaveCount(0);
  await expect(page.getByTestId("memory-facts-panel")).toBeVisible();

  await page.getByTestId("memory-filter-summaries").click();
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
  await expect(page.getByTestId("memory-facts-panel")).toHaveCount(0);
});
```

- [ ] **Step 2: 运行 E2E 并确认因新工作台标识不存在而失败**

Run: `cd frontend && pnpm test:e2e tests/e2e/memory-workbench.spec.ts`

Expected: FAIL，`memory-workbench` 或面板 test id 不存在。

- [ ] **Step 3: 为能力页面增加显式宽度变体**

将 `WorkspaceCapabilityPage` 签名改为：

```tsx
export function WorkspaceCapabilityPage({
  children,
  width = "default",
}: Readonly<{
  children: React.ReactNode;
  width?: "default" | "wide";
}>) {
  return (
    <WorkspaceContainer>
      <WorkspaceHeader />
      <WorkspaceBody className="overflow-y-auto">
        <div
          className={cn(
            "w-full p-4 sm:p-6 md:p-8",
            width === "wide" ? "max-w-[1440px]" : "max-w-5xl",
          )}
        >
          {children}
        </div>
      </WorkspaceBody>
    </WorkspaceContainer>
  );
}
```

从 `@/lib/utils` 导入 `cn`，并在 memory route 使用 `<WorkspaceCapabilityPage width="wide">`。工具和技能页面不传属性，保持当前宽度。

- [ ] **Step 4: 将记忆内容重排为专注工作台**

在 `MemorySettingsPage` 中保留所有现有 hooks、处理函数和弹窗，只替换主 `return` 中 `SettingsSection` 的页面内容：

```tsx
<section data-testid="memory-workbench" className="space-y-6">
  <header className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
    <div className="space-y-1">
      <h1 className="text-2xl font-semibold tracking-tight">{t.settings.memory.title}</h1>
      <p className="text-muted-foreground max-w-2xl text-sm">{t.settings.memory.description}</p>
    </div>
    <div className="flex flex-wrap items-center gap-2">
      {/* 保留现有 hidden file input、导入、导出、添加事实和清空按钮及处理函数 */}
    </div>
  </header>

  <div className="bg-muted/25 flex flex-col gap-3 rounded-xl border p-3 sm:flex-row sm:items-center">
    {/* 保留现有搜索 Input */}
    <ToggleGroup>{/* 保留现有 filter，只增加三个 data-testid */}</ToggleGroup>
  </div>

  <div className={cn("grid gap-4", filter === "all" && "lg:grid-cols-[minmax(0,0.36fr)_minmax(0,0.64fr)]")}>
    {shouldRenderSummariesBlock ? (
      <section data-testid="memory-summary-panel" className="bg-card min-w-0 rounded-xl border p-5 shadow-xs">
        {/* 保留 summaryReadOnly 与 SafeStreamdown 渲染 */}
      </section>
    ) : null}
    {shouldRenderFactsBlock ? (
      <section data-testid="memory-facts-panel" className="bg-card min-w-0 rounded-xl border p-5 shadow-xs">
        {/* 保留 filteredFacts、来源链接、编辑和删除按钮 */}
      </section>
    ) : null}
  </div>
</section>
```

事实行使用 `border-border/70 hover:bg-muted/30 rounded-lg border p-4 transition-colors`；摘要/事实单独筛选时因只有一个 child 自动占满全宽。加载、错误、空数据、无匹配结果和全部弹窗保持现有判断与调用。

- [ ] **Step 5: 运行记忆 E2E 并确认通过**

Run: `cd frontend && pnpm test:e2e tests/e2e/memory-workbench.spec.ts`

Expected: PASS。

- [ ] **Step 6: 运行前端静态检查并提交**

Run: `cd frontend && pnpm check`

Expected: ESLint 与 TypeScript 均退出码 0。

```bash
git add frontend/tests/e2e/memory-workbench.spec.ts frontend/src/components/workspace/workspace-capability-page.tsx frontend/src/app/workspace/memory/page.tsx frontend/src/components/workspace/settings/memory-settings-page.tsx
git commit -m "feat(frontend): redesign memory as a focused workbench"
```

---

### Task 2: 定时任务创建抽屉与主从工作台

**Files:**
- Modify: `frontend/tests/e2e/scheduled-tasks.spec.ts`
- Modify: `frontend/src/app/workspace/scheduled-tasks/page.tsx`

**Interfaces:**
- Produces: `createSheetOpen: boolean` UI state；`data-testid="scheduled-task-create-trigger"`。
- Consumes: 现有 `createTask` mutation、表单 state、`RECIPES`、`ScheduledTaskScheduleInput`、筛选 state、选中回退 effect 和所有现有 test ids。
- Preserves: `scheduled-task-create-form`、`scheduled-task-list`、`scheduled-task-detail`、`scheduled-task-runs`、`scheduled-task-run-list`。

- [ ] **Step 1: 先更新创建任务 E2E 以描述抽屉行为**

将现有 `user can create a scheduled task from the page` 测试的开头改为：

```ts
await page.goto("/workspace/scheduled-tasks");
await expect(page.getByTestId("scheduled-task-create-form")).toHaveCount(0);
await page.getByTestId("scheduled-task-create-trigger").click();

const createSheet = page.getByRole("dialog", {
  name: "Create scheduled task",
});
await expect(createSheet).toBeVisible();
const createForm = createSheet.getByTestId("scheduled-task-create-form");
```

保留原字段填写和创建按钮操作，并在提交后追加：

```ts
await expect(createSheet).toHaveCount(0);
```

同时在第一个可达性测试中断言 `scheduled-task-list` 和 `scheduled-task-detail` 可见。

- [ ] **Step 2: 运行定时任务 E2E 并确认因触发器不存在或表单常驻而失败**

Run: `cd frontend && pnpm test:e2e tests/e2e/scheduled-tasks.spec.ts`

Expected: FAIL，`scheduled-task-create-form` 仍常驻或 `scheduled-task-create-trigger` 不存在。

- [ ] **Step 3: 增加受控右侧创建抽屉**

导入现有 Sheet 组件：

```tsx
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
```

新增：

```ts
const [createSheetOpen, setCreateSheetOpen] = useState(false);
```

页面头部主按钮：

```tsx
<Button data-testid="scheduled-task-create-trigger" onClick={() => setCreateSheetOpen(true)}>
  <PlusIcon className="size-4" />
  {st.create.title}
</Button>
```

将原 `scheduled-task-create-form` 整块移动到：

```tsx
<Sheet open={createSheetOpen} onOpenChange={setCreateSheetOpen}>
  <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
    <SheetHeader className="border-b px-6 py-5">
      <SheetTitle>{st.create.title}</SheetTitle>
      <SheetDescription>{t.sidebar.scheduledTasks}</SheetDescription>
    </SheetHeader>
    <div className="space-y-5 px-6 pb-8" data-testid="scheduled-task-create-form">
      {/* 原配方、上下文模式、字段、ScheduleInput、错误和提交按钮 */}
    </div>
  </SheetContent>
</Sheet>
```

在现有 `createTask.mutate(..., { onSuccess })` 中保留全部清理语句，并在最后添加 `setCreateSheetOpen(false)`。失败路径不关闭抽屉。

- [ ] **Step 4: 将主页面重排为 36/64 工作台**

保留全部现有派生值和事件处理，只重排页面 JSX：

```tsx
<WorkspaceBody className="overflow-y-auto">
  <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 p-4 sm:p-6 md:p-8">
    <header className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">{t.sidebar.scheduledTasks}</h1>
        {threadId ? <p className="text-muted-foreground text-sm">...</p> : null}
      </div>
      {/* 新建任务按钮 */}
    </header>

    <section className="bg-muted/25 flex flex-wrap gap-3 rounded-xl border p-3">
      {/* 原 statusFilter 与 typeFilter 按钮，分成两个有小标题的组 */}
    </section>

    <div className="grid items-start gap-4 lg:grid-cols-[minmax(280px,0.36fr)_minmax(0,0.64fr)]">
      <section className="bg-card rounded-xl border p-3 shadow-xs" data-testid="scheduled-task-list">
        {/* 原 filteredData map；卡片补充 next run，保留选择按钮和 test ids */}
      </section>
      <section className="bg-card rounded-xl border p-5 shadow-xs" data-testid="scheduled-task-detail">
        {/* 原详情、编辑、动作与运行记录；元数据改两列 grid，逻辑不变 */}
      </section>
    </div>
  </div>
</WorkspaceBody>
```

状态必须同时保留文本；可使用现有 `Badge` 的 `outline`/`secondary`/`destructive` 变体和主题色 class。错误状态继续显示 `scheduled-task-load-error`。

- [ ] **Step 5: 运行全部定时任务 E2E 并确认核心行为未回归**

Run: `cd frontend && pnpm test:e2e tests/e2e/scheduled-tasks.spec.ts`

Expected: 可达性、线程过滤、创建、暂停、立即触发、运行记录、筛选回退全部 PASS。

- [ ] **Step 6: 运行静态检查并提交**

Run: `cd frontend && pnpm check`

Expected: ESLint 与 TypeScript 均退出码 0。

```bash
git add frontend/tests/e2e/scheduled-tasks.spec.ts frontend/src/app/workspace/scheduled-tasks/page.tsx
git commit -m "feat(frontend): redesign scheduled tasks workbench"
```

---

### Task 3: 文档同步、响应式验收与完整验证

**Files:**
- Modify: `frontend/AGENTS.md`

**Interfaces:**
- Consumes: Task 1 的宽度变体和记忆面板契约、Task 2 的创建 Sheet 与定时任务工作台。
- Produces: 前端架构指南中的页面所有权说明和完整验证证据。

- [ ] **Step 1: 更新前端交互所有权说明**

在 `frontend/AGENTS.md` 的 `Interaction Ownership` 中增加：

```md
- `src/app/workspace/scheduled-tasks/page.tsx` owns scheduled-task filters, selection, mutations, and the controlled create-task Sheet; the Sheet is presentation only and must reuse the page's existing payload and reset flow.
- `src/components/workspace/settings/memory-settings-page.tsx` owns memory filters and mutations; its wide workbench panels are presentation-only views over the existing filtered summaries and facts.
```

- [ ] **Step 2: 运行相关 E2E**

Run:

```bash
cd frontend && pnpm test:e2e tests/e2e/memory-workbench.spec.ts tests/e2e/scheduled-tasks.spec.ts
```

Expected: 两个 spec 全部 PASS。

- [ ] **Step 3: 运行全量单元测试和静态检查**

Run:

```bash
cd frontend
pnpm test
pnpm check
pnpm format
```

Expected: 单元测试 0 失败，ESLint/TypeScript 退出码 0，Prettier 通过。

- [ ] **Step 4: 浏览器验收**

在已登录的 `http://localhost:2026` 验证：

1. `1440px` 级桌面视口下，两页为 36/64 主从分栏。
2. 定时任务创建表单默认隐藏，右侧 Sheet 可打开、关闭并在成功创建后自动关闭。
3. 记忆“全部 / 事实 / 摘要”正确切换面板，搜索、导入、导出、添加、编辑、删除和清空入口仍可访问。
4. 窄屏下两页为单列且无横向溢出。
5. 浅色与深色模式中的边框、文字和状态徽标可辨识。

- [ ] **Step 5: 提交文档并确认工作树**

```bash
git add frontend/AGENTS.md
git commit -m "docs(frontend): document workbench ownership"
git status --short
```

Expected: 本功能文件全部已提交，工作树没有本功能遗留修改。
