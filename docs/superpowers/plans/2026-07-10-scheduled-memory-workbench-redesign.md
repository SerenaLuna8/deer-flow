# Scheduled Tasks and Memory Workbench Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将定时任务与记忆页面重排为宽屏、低噪音的专注工作台，同时保持所有数据、校验、mutation、权限和后端契约不变。

**Architecture:** 页面继续拥有现有 hooks 与业务状态；新增的代码只负责宽内容容器、记忆响应式面板和定时任务 Sheet/主从排版。记忆的 `all / facts / summaries` 展示规则提取为纯展示组件，定时任务的创建逻辑仍留在原页面，只把表单节点移动到受控 Sheet。

**Tech Stack:** Next.js 16 App Router、React 19、TypeScript 5.8、Tailwind CSS 4、shadcn/Radix UI、Rstest、Playwright、pnpm。

## Global Constraints

- 桌面内容最大宽度约 `1440px`，桌面主从比例约 35/65，`lg` 以下单列。
- 定时任务创建表单必须位于右侧 Sheet；关闭未提交 Sheet 不清空输入，成功创建后重置并关闭。
- 保留现有 Scheduled Task 与 Memory hooks、API、类型、缓存键、mutation、业务校验和错误语义。
- 保留现有对外 `data-testid`，仅允许测试先点击“创建定时任务”打开表单。
- 记忆 `all` 显示摘要与事实双栏，`facts` 和 `summaries` 各自显示全宽单栏。
- 不新增任务搜索、排序、分页、统计卡片或新的过滤条件。
- 不引入依赖、设计系统、主题或动画框架。
- 生产代码必须在对应失败测试之后编写。

---

### Task 1: 支持记忆页宽工作区

**Files:**
- Modify: `frontend/src/components/workspace/workspace-capability-page.tsx`
- Modify: `frontend/src/app/workspace/memory/page.tsx`
- Modify: `frontend/tests/unit/app/workspace/capability-pages.test.ts`

**Interfaces:**
- Produces: `WorkspaceCapabilityPage({ children, contentClassName })`，其中 `contentClassName?: string`。
- Consumes: 现有 `cn()` 与 Memory route。

- [ ] **Step 1: 添加失败测试，要求记忆路由申请宽内容容器**

在现有页面映射测试中增加：

```ts
it("gives the memory workbench a wide content container", () => {
  const element = MemoryPage() as ReactElement<{
    children: ReactElement;
    contentClassName?: string;
  }>;
  expect(element.props.contentClassName).toContain("max-w-[1440px]");
});
```

- [ ] **Step 2: 运行测试并确认因 prop 尚不存在而失败**

Run: `cd frontend && pnpm test tests/unit/app/workspace/capability-pages.test.ts`

Expected: FAIL，`contentClassName` 为 `undefined`。

- [ ] **Step 3: 实现可覆盖内容宽度的容器**

将组件改为：

```tsx
import {
  WorkspaceBody,
  WorkspaceContainer,
  WorkspaceHeader,
} from "@/components/workspace/workspace-container";
import { cn } from "@/lib/utils";

export function WorkspaceCapabilityPage({
  children,
  contentClassName,
}: Readonly<{
  children: React.ReactNode;
  contentClassName?: string;
}>) {
  return (
    <WorkspaceContainer>
      <WorkspaceHeader />
      <WorkspaceBody className="overflow-y-auto">
        <div
          className={cn("w-full max-w-5xl p-6 md:p-8", contentClassName)}
        >
          {children}
        </div>
      </WorkspaceBody>
    </WorkspaceContainer>
  );
}
```

记忆路由调用：

```tsx
<WorkspaceCapabilityPage contentClassName="max-w-[1440px]">
  <MemorySettingsPage />
</WorkspaceCapabilityPage>
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `cd frontend && pnpm test tests/unit/app/workspace/capability-pages.test.ts`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/workspace/workspace-capability-page.tsx frontend/src/app/workspace/memory/page.tsx frontend/tests/unit/app/workspace/capability-pages.test.ts
git commit -m "refactor(frontend): widen memory workspace"
```

---

### Task 2: 重设计记忆专注工作台

**Files:**
- Create: `frontend/src/components/workspace/settings/memory-workbench.tsx`
- Create: `frontend/tests/unit/components/workspace/settings/memory-workbench.test.ts`
- Modify: `frontend/src/components/workspace/settings/memory-settings-page.tsx`

**Interfaces:**
- Produces: `MemoryViewFilter = "all" | "facts" | "summaries"`。
- Produces: `MemoryWorkbench({ filter, summaryTitle, summaryDescription, summaries, factsTitle, factsCount, facts })`。
- Consumes: 已过滤的 summary/fact React 节点；不读取 API 或 hooks。

- [ ] **Step 1: 写失败测试，锁定双栏与单栏规则**

```ts
import { describe, expect, it } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { MemoryWorkbench } from "@/components/workspace/settings/memory-workbench";

function render(filter: "all" | "facts" | "summaries") {
  return renderToStaticMarkup(
    createElement(MemoryWorkbench, {
      filter,
      summaryTitle: "Summaries",
      summaryDescription: "Read only",
      summaries: createElement("div", null, "summary-content"),
      factsTitle: "Facts",
      factsCount: 2,
      facts: createElement("div", null, "facts-content"),
    }),
  );
}

describe("MemoryWorkbench", () => {
  it("renders summaries and facts in the all view", () => {
    const html = render("all");
    expect(html).toContain('data-testid="memory-summary-panel"');
    expect(html).toContain('data-testid="memory-facts-panel"');
    expect(html).toContain("lg:grid-cols-[minmax(280px,0.36fr)_minmax(0,0.64fr)]");
  });

  it("renders only the selected full-width panel", () => {
    expect(render("facts")).not.toContain('data-testid="memory-summary-panel"');
    expect(render("facts")).toContain('data-testid="memory-facts-panel"');
    expect(render("summaries")).toContain('data-testid="memory-summary-panel"');
    expect(render("summaries")).not.toContain('data-testid="memory-facts-panel"');
  });
});
```

- [ ] **Step 2: 运行测试并确认因组件不存在而失败**

Run: `cd frontend && pnpm test tests/unit/components/workspace/settings/memory-workbench.test.ts`

Expected: FAIL，无法解析 `memory-workbench`。

- [ ] **Step 3: 实现纯展示工作台组件**

```tsx
import { cn } from "@/lib/utils";

export type MemoryViewFilter = "all" | "facts" | "summaries";

export function MemoryWorkbench({
  filter,
  summaryTitle,
  summaryDescription,
  summaries,
  factsTitle,
  factsCount,
  facts,
}: {
  filter: MemoryViewFilter;
  summaryTitle: React.ReactNode;
  summaryDescription: React.ReactNode;
  summaries: React.ReactNode | null;
  factsTitle: React.ReactNode;
  factsCount: number;
  facts: React.ReactNode | null;
}) {
  const showSummaries = filter !== "facts" && summaries !== null;
  const showFacts = filter !== "summaries" && facts !== null;
  return (
    <div
      className={cn(
        "grid min-w-0 gap-4",
        filter === "all" &&
          "lg:grid-cols-[minmax(280px,0.36fr)_minmax(0,0.64fr)]",
      )}
      data-testid="memory-workbench"
    >
      {showSummaries && (
        <section
          className="bg-card min-w-0 rounded-xl border p-4 shadow-xs"
          data-testid="memory-summary-panel"
        >
          <header className="mb-4 space-y-1">
            <h2 className="font-semibold">{summaryTitle}</h2>
            <p className="text-muted-foreground text-xs">
              {summaryDescription}
            </p>
          </header>
          {summaries}
        </section>
      )}
      {showFacts && (
        <section
          className="bg-card min-w-0 rounded-xl border p-4 shadow-xs"
          data-testid="memory-facts-panel"
        >
          <header className="mb-4 flex items-center justify-between gap-3">
            <h2 className="font-semibold">{factsTitle}</h2>
            <span className="text-muted-foreground text-xs">{factsCount}</span>
          </header>
          {facts}
        </section>
      )}
    </div>
  );
}
```

- [ ] **Step 4: 将记忆页排版迁入工作台，不改变 handlers**

在 `memory-settings-page.tsx` 中执行以下精确结构调整：

1. 从 `memory-workbench.tsx` 导入 `MemoryWorkbench` 和 `MemoryViewFilter`，删除本地同名类型。
2. 删除 `SettingsSection` 和 `summariesToMarkdown()`；保留 `formatMemorySection()`、过滤计算、所有 handlers 与 Dialog。
3. 新增 `Badge`、`MoreHorizontalIcon`、DropdownMenu 组件导入。
4. 根内容改为一个 `<section className="space-y-6">`。首个 header 使用 `flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between`：左侧展示现有 title/description，右侧依次保留隐藏 file input、导入、导出、添加事实和“更多”菜单；“更多”中仅放现有清空操作并调用 `setClearDialogOpen(true)`。
5. 搜索和 ToggleGroup 放入 `bg-card rounded-xl border p-3 shadow-xs` 工具栏，保留 value、onChange 与过滤值。
6. 将现有无匹配提示放在工具栏之后。
7. 使用 `MemoryWorkbench`：`filter={filter}`，summary title 使用 `filterSummaries`，description 使用 `summaryReadOnly`，facts title 使用 `t.settings.memory.markdown.facts`，factsCount 使用 `filteredFacts.length`。`summaries` 在 `shouldRenderSummariesBlock` 为 false 时传 `null`，`facts` 在 `shouldRenderFactsBlock` 为 false 时传 `null`，保留现有搜索结果可见性。
8. summaries 节点按 `filteredSectionGroups` 渲染：每组显示弱化的小标题；每个 section 使用 `rounded-lg border bg-muted/20 p-3`，标题与更新时间位于顶部，正文继续通过 `SafeStreamdown` 渲染 `section.summary || t.settings.memory.markdown.empty`。
9. facts 节点继续映射 `filteredFacts`，把每条调整为 `group rounded-lg border p-4`；正文置顶，category/confidence 使用 `Badge variant="secondary"`，createdAt/source 使用弱化文本，现有编辑/删除按钮、aria-label、disabled 和 callbacks 原样保留。
10. 加载、错误、完全空、无摘要、无事实和所有 Dialog 的现有条件及文案保持不变。

- [ ] **Step 5: 运行记忆布局测试与既有相关测试**

Run:

```bash
cd frontend && pnpm test \
  tests/unit/components/workspace/settings/memory-workbench.test.ts \
  tests/unit/app/workspace/capability-pages.test.ts
```

Expected: PASS。

- [ ] **Step 6: 运行静态检查并提交**

Run: `cd frontend && pnpm check`

Expected: ESLint 与 TypeScript 退出码 0。

```bash
git add frontend/src/components/workspace/settings/memory-workbench.tsx frontend/src/components/workspace/settings/memory-settings-page.tsx frontend/tests/unit/components/workspace/settings/memory-workbench.test.ts
git commit -m "feat(frontend): redesign memory workbench"
```

---

### Task 3: 重设计定时任务工作台与创建抽屉

**Files:**
- Modify: `frontend/src/app/workspace/scheduled-tasks/page.tsx`
- Modify: `frontend/tests/e2e/scheduled-tasks.spec.ts`

**Interfaces:**
- Consumes: 现有 Scheduled Task hooks、state、handlers、`ScheduledTaskScheduleInput` 与所有 test IDs。
- Produces: 受控 `Sheet` 创建流程与宽屏主从布局；不产生新的业务 API。

- [ ] **Step 1: 先修改创建 E2E，要求通过按钮打开 Sheet**

将创建测试开头改为：

```ts
await page.goto("/workspace/scheduled-tasks");
await expect(page.getByTestId("scheduled-task-create-form")).toHaveCount(0);
await page
  .getByRole("button", { name: "Create scheduled task", exact: true })
  .click();
const createForm = page.getByTestId("scheduled-task-create-form");
await expect(createForm).toBeVisible();
```

在同一 spec 增加草稿保留测试：

```ts
test("closing the create sheet keeps the draft", async ({ page }) => {
  mockLangGraphAPI(page, { threads: [], scheduledTasks: [] });
  await page.goto("/workspace/scheduled-tasks");
  await page
    .getByRole("button", { name: "Create scheduled task", exact: true })
    .click();
  await page.getByPlaceholder("Task title").fill("Keep this draft");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page
    .getByRole("button", { name: "Create scheduled task", exact: true })
    .click();
  await expect(page.getByPlaceholder("Task title")).toHaveValue(
    "Keep this draft",
  );
});
```

- [ ] **Step 2: 运行两个创建测试并确认旧页面失败**

Run: `cd frontend && pnpm test:e2e tests/e2e/scheduled-tasks.spec.ts --grep "create|draft"`

Expected: FAIL，因为旧页面没有“Create scheduled task”按钮，且表单常驻。

- [ ] **Step 3: 增加受控 Sheet，不改变创建逻辑**

在页面 state 中增加：

```ts
const [createOpen, setCreateOpen] = useState(false);
```

导入并使用：

```tsx
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
```

在现有 `data-testid="scheduled-task-create-form"` 节点之前插入以下 Sheet 开始结构：

```tsx
<Sheet open={createOpen} onOpenChange={setCreateOpen}>
  <SheetContent className="w-full p-0 sm:max-w-xl">
    <SheetHeader className="border-b px-6 py-5">
      <SheetTitle>{st.create.title}</SheetTitle>
      <SheetDescription>{t.sidebar.scheduledTasks}</SheetDescription>
    </SheetHeader>
    <ScrollArea className="min-h-0 flex-1">
```

将原创建表单整体移动到这个内层，并把 class 改为 `grid gap-5 px-6 py-5`。在原创建按钮之前增加：

```tsx
<Button variant="outline" onClick={() => setCreateOpen(false)}>
  {t.common.cancel}
</Button>
```

将 Cancel 与原创建按钮共同包入 `className="sticky bottom-0 -mx-6 -mb-5 flex justify-end gap-2 border-t bg-background px-6 py-4"` 的容器。在原创建表单闭合标签之后追加：

```tsx
    </ScrollArea>
  </SheetContent>
</Sheet>
```

现有 Create Button 的 `onClick`、payload 和 disabled 表达式逐字保留；其 `onSuccess` 在现有字段重置之后增加 `setCreateOpen(false)`。Sheet 普通关闭和 Cancel 只设置 `createOpen=false`，不得重置字段。

- [ ] **Step 4: 重排主工作台**

将 `WorkspaceBody` 内内容容器改为：

```tsx
<div className="mx-auto flex w-full max-w-[1440px] flex-col gap-6 p-6 md:p-8">
```

页面结构顺序固定为：

1. header：左侧现有标题，右侧 `<Button onClick={() => setCreateOpen(true)}>{st.create.title}</Button>`。
2. thread filter 与 query error 提示。
3. 过滤 toolbar：状态按钮与类型按钮分成两个可换行组，保留现有 filter state 和 callbacks。
4. `lg:grid-cols-[minmax(300px,0.36fr)_minmax(0,0.64fr)]` 主从网格。

左侧列表容器使用 `bg-card rounded-xl border p-3 shadow-xs`。每个现有任务按钮保留 test ID、key、onClick；class 调整为：

```tsx
cn(
  "focus-visible:ring-ring w-full rounded-lg border p-3 text-left transition-colors focus-visible:ring-2 focus-visible:outline-none",
  isSelected
    ? "border-primary/40 bg-primary/5"
    : "border-transparent hover:border-border hover:bg-muted/40",
)
```

条目显示 title、status `Badge`、scheduleType、nextRun；不增加新字段或排序。

右侧详情容器保留 `data-testid="scheduled-task-detail"`，使用 `bg-card min-w-0 rounded-xl border p-5 shadow-xs lg:sticky lg:top-4 lg:self-start`。内部按规格重排为标题/状态、2–3 列 metadata grid、prompt 或原编辑表单、动作区、Separator、运行历史。所有 update/pause/resume/trigger/delete callbacks 与 pending 禁用逻辑原样保留。

- [ ] **Step 5: 运行完整 Scheduled Tasks E2E**

Run: `cd frontend && pnpm test:e2e tests/e2e/scheduled-tasks.spec.ts`

Expected: 全部 PASS，包括创建抽屉、草稿保留、筛选回退、暂停与立即触发。

- [ ] **Step 6: 运行静态检查并提交**

Run: `cd frontend && pnpm check`

Expected: ESLint 与 TypeScript 退出码 0。

```bash
git add frontend/src/app/workspace/scheduled-tasks/page.tsx frontend/tests/e2e/scheduled-tasks.spec.ts
git commit -m "feat(frontend): redesign scheduled tasks workbench"
```

---

### Task 4: 完整验证与视觉回归检查

**Files:**
- No production file changes expected.

**Interfaces:**
- Consumes: Tasks 1–3 的最终页面。
- Produces: 可交付的验证证据。

- [ ] **Step 1: 运行全部前端单元测试**

Run: `cd frontend && pnpm test`

Expected: 0 failed files，0 failed tests。

- [ ] **Step 2: 运行 Scheduled Tasks E2E**

Run: `cd frontend && pnpm test:e2e tests/e2e/scheduled-tasks.spec.ts`

Expected: 0 failures。

- [ ] **Step 3: 运行静态与格式检查**

Run:

```bash
cd frontend && pnpm check
cd frontend && pnpm format
git diff --check
```

Expected: 所有命令退出码 0；若 Prettier 失败，仅格式化本功能变更文件并重新执行检查。

- [ ] **Step 4: 核对业务逻辑边界**

使用 `git diff` 确认以下文件未修改：

```text
frontend/src/core/scheduled-tasks/**
frontend/src/core/memory/**
backend/**
```

确认 Scheduled Task payload、Memory mutation 输入、API 路径和类型均未变化。

- [ ] **Step 5: 浏览器验收**

在可认证本地会话中检查：

1. `/workspace/scheduled-tasks` 为宽工作区，创建按钮打开右侧 Sheet。
2. Sheet 关闭后草稿保留，创建成功后关闭。
3. 任务列表与详情在桌面双栏、窄屏单栏。
4. `/workspace/memory` 的 `all` 为摘要/事实双栏，单项过滤为全宽。
5. 记忆导入、导出、添加事实与更多菜单可访问，所有 Dialog 正常。
6. 浅色/深色主题无硬编码冲突，页面无水平溢出。

若浏览器会话因认证不可用，在报告中明确记录，并以自动化测试、静态检查和本地服务 HTTP 结果作为证据。

- [ ] **Step 6: 确认工作树状态**

Run: `git status --short --branch`

Expected: 本功能文件均已提交，工作树不包含意外改动。
