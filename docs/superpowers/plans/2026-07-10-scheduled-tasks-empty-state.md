# Scheduled Tasks Empty-State Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用统一引导空状态替换定时任务页面的两个空框，并简化有数据状态下的筛选层级，同时保持全部业务逻辑和后端契约不变。

**Architecture:** `ScheduledTasksPage` 继续持有现有 hooks、状态、派生数据和 mutations；新增的 `hasNoTasks` 与 `hasFilterMiss` 只决定 JSX 呈现。空状态复用现有 shadcn `Empty` 组件，创建按钮继续打开同一个受控 Sheet，清除筛选只重置已有两个筛选 state。

**Tech Stack:** Next.js 16、React 19、TypeScript 5.8、Tailwind CSS 4、shadcn/Radix、Playwright、Rstest、pnpm。

## Global Constraints

- 不修改后端 API、scheduler、任务 schema、hooks、cache key 或 mutation payload。
- 不改变创建、编辑、暂停、恢复、触发、删除、选择回退或运行记录逻辑。
- 零任务时隐藏筛选栏、列表和详情；筛选无匹配时保留筛选栏并提供清除操作。
- 有可见任务时继续使用现有 36/64 工作台。
- 筛选按钮继续提供 `aria-pressed`，移动端不得产生水平溢出。
- 不新增第三方依赖。

---

### Task 1: 统一空状态与筛选层级

**Files:**
- Modify: `frontend/tests/e2e/scheduled-tasks.spec.ts`
- Modify: `frontend/src/app/workspace/scheduled-tasks/page.tsx`
- Modify: `frontend/src/core/i18n/locales/en-US.ts`
- Modify: `frontend/src/core/i18n/locales/zh-CN.ts`
- Modify: `frontend/src/core/i18n/locales/types.ts`

**Interfaces:**
- Consumes: 现有 `data`、`filteredData`、`statusFilter`、`typeFilter`、`setCreateSheetOpen` 和受控创建 Sheet。
- Produces: `data-testid="scheduled-task-empty"`、`scheduled-task-empty-action`、`scheduled-task-filter-empty`、`scheduled-task-clear-filters`、`scheduled-task-filters`。
- Preserves: `scheduled-task-create-trigger`、`scheduled-task-create-form`、`scheduled-task-workbench`、`scheduled-task-list`、`scheduled-task-detail` 和所有 mutation payload。

- [ ] **Step 1: 写零任务失败 E2E**

在 `scheduled-tasks.spec.ts` 中新增：

```ts
test("empty page guides the user to create the first scheduled task", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  mockLangGraphAPI(page, { threads: [], scheduledTasks: [] });
  await page.goto("/workspace/scheduled-tasks");

  await expect(page.getByTestId("scheduled-task-empty")).toBeVisible();
  await expect(page.getByTestId("scheduled-task-filters")).toHaveCount(0);
  await expect(page.getByTestId("scheduled-task-workbench")).toHaveCount(0);
  await expect(page.getByTestId("scheduled-task-list")).toHaveCount(0);
  await expect(page.getByTestId("scheduled-task-detail")).toHaveCount(0);

  await page.getByTestId("scheduled-task-empty-action").click();
  await expect(page.getByRole("dialog", { name: "Create scheduled task" })).toBeVisible();
});
```

- [ ] **Step 2: 写筛选无匹配失败 E2E**

使用一条 `enabled + cron` fixture，点击失败状态后断言筛选空状态，再清除：

```ts
const statusGroup = page.getByRole("group", { name: "Status" });
await statusGroup.getByRole("button", { name: "Failed" }).click();
await expect(page.getByTestId("scheduled-task-filter-empty")).toBeVisible();
await expect(page.getByTestId("scheduled-task-workbench")).toHaveCount(0);

await page.getByTestId("scheduled-task-clear-filters").click();
await expect(page.getByTestId("scheduled-task-filter-empty")).toHaveCount(0);
await expect(page.getByTestId("scheduled-task-list")).toBeVisible();
await expect(page.getByTestId("scheduled-task-detail")).toBeVisible();
```

- [ ] **Step 3: 运行目标 E2E 并确认 RED**

Run:

```bash
cd frontend
DEER_FLOW_AUTH_DISABLED=1 SKIP_ENV_VALIDATION=1 pnpm dev --port 3101
PLAYWRIGHT_SKIP_WEB_SERVER=1 PLAYWRIGHT_BASE_URL=http://localhost:3101 pnpm test:e2e tests/e2e/scheduled-tasks.spec.ts
```

Expected: FAIL，`scheduled-task-empty` 或 `scheduled-task-filter-empty` 不存在；既有页面仍渲染空列表和详情。

- [ ] **Step 4: 增加精确 i18n 文案**

在 `Translations.scheduledTasks` 及中英文 locale 中增加：

```ts
description: string;
filters: {
  status: string;
  type: string;
  all: string;
  // 保留现有字段
};
empty: {
  title: string;
  description: string;
  action: string;
  filteredTitle: string;
  filteredDescription: string;
  clearFilters: string;
};
```

英文使用 `Run tasks on a schedule and review every result.`、`No scheduled tasks yet`、`Create your first task`；中文使用“按计划自动运行任务，并查看每次执行结果。”“还没有定时任务”“创建第一条任务”。筛选分组使用 `Status / Type / All` 与“状态 / 类型 / 全部”。

- [ ] **Step 5: 增加纯展示派生状态**

在现有 `filteredData` 后新增：

```ts
const hasLoadedTasks = data !== undefined;
const hasNoTasks = hasLoadedTasks && data.length === 0;
const hasFilterMiss = (data?.length ?? 0) > 0 && filteredData.length === 0;
```

这些值只能控制 JSX，不得进入 hooks、mutation 或请求参数。

- [ ] **Step 6: 实现零任务 Empty**

从 `lucide-react` 导入 `CalendarClockIcon`，并导入现有 Empty 组件：

```tsx
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
```

标题区增加 `<p>{st.description}</p>`。当 `hasNoTasks` 时渲染：

```tsx
<Empty
  data-testid="scheduled-task-empty"
  className="bg-card/60 min-h-[320px] border"
>
  <EmptyHeader>
    <EmptyMedia variant="icon"><CalendarClockIcon /></EmptyMedia>
    <EmptyTitle>{st.empty.title}</EmptyTitle>
    <EmptyDescription>{st.empty.description}</EmptyDescription>
  </EmptyHeader>
  <EmptyContent>
    <Button
      data-testid="scheduled-task-empty-action"
      onClick={() => setCreateSheetOpen(true)}
    >
      <PlusIcon className="size-4" />
      {st.empty.action}
    </Button>
  </EmptyContent>
</Empty>
```

该分支不得渲染筛选栏或工作台。

- [ ] **Step 7: 简化筛选与实现筛选空状态**

筛选栏增加 `data-testid="scheduled-task-filters"`。分组标签和 aria-label 改为 `st.filters.status` / `st.filters.type`，两个“全部”按钮统一显示 `st.filters.all`。按钮 variant 改为：

```tsx
variant={active ? "secondary" : "ghost"}
```

当 `hasFilterMiss` 时，在筛选栏之后渲染：

```tsx
<Empty
  data-testid="scheduled-task-filter-empty"
  className="bg-card min-h-[240px] border"
>
  <EmptyHeader>
    <EmptyTitle>{st.empty.filteredTitle}</EmptyTitle>
    <EmptyDescription>{st.empty.filteredDescription}</EmptyDescription>
  </EmptyHeader>
  <EmptyContent>
    <Button
      variant="outline"
      data-testid="scheduled-task-clear-filters"
      onClick={() => {
        setStatusFilter("all");
        setTypeFilter("all");
      }}
    >
      {st.empty.clearFilters}
    </Button>
  </EmptyContent>
</Empty>
```

只有 `filteredData.length > 0` 时渲染现有工作台。

- [ ] **Step 8: 更新既有筛选测试并运行 GREEN**

将现有 E2E 中 group 名称改为 `Status` 与 `Type`，全部按钮名改为 `All`。运行：

```bash
cd frontend
PLAYWRIGHT_SKIP_WEB_SERVER=1 PLAYWRIGHT_BASE_URL=http://localhost:3101 pnpm test:e2e tests/e2e/scheduled-tasks.spec.ts
```

Expected: 新旧测试全部 PASS。

- [ ] **Step 9: 运行完整前端验证**

```bash
cd frontend
pnpm test
pnpm check
pnpm format
git diff --check
```

Expected: 592 个单元测试 0 失败，ESLint/TypeScript/Prettier 全部退出码 0。

- [ ] **Step 10: 提交**

```bash
git add frontend/tests/e2e/scheduled-tasks.spec.ts frontend/src/app/workspace/scheduled-tasks/page.tsx frontend/src/core/i18n/locales/en-US.ts frontend/src/core/i18n/locales/zh-CN.ts frontend/src/core/i18n/locales/types.ts
git commit -m "feat(frontend): improve scheduled task empty states"
```
