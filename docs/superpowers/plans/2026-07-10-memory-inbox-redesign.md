# Memory Inbox Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `/workspace/memory` 实现为事实优先的“记忆收件箱”，保留全部现有记忆 API、mutation、校验、导入导出格式和来源跳转行为。

**Architecture:** `MemorySettingsPage` 继续作为 controller，持有查询、搜索、筛选、文件输入、弹窗和 mutations。纯派生逻辑提取到 `memory-view-model.ts`，页面视觉区域提取到 `memory-workbench.tsx`；展示组件只接收数据与回调，不调用 API。Playwright 通过一个可变的内存 API mock 验证所有入口仍走原有 HTTP 契约。

**Tech Stack:** Next.js 16 App Router、React 19、TypeScript 5.8、Tailwind CSS 4、现有 shadcn/Radix 组件、Lucide React、TanStack Query、Rstest、Playwright、pnpm 10.26.2。

## Global Constraints

- 不修改 backend、`frontend/src/core/memory/api.ts`、`frontend/src/core/memory/hooks.ts`、`UserMemory` schema、query key 或 mutation payload。
- 事实始终是主内容，宽度在所有断点均为 `100%`；不再使用 `36% / 64%` 摘要/事实分栏。
- 摘要保持只读并继续通过 `SafeStreamdown` 渲染已有 Markdown。
- `All` 显示事实和摘要 disclosure，`Facts` 只显示事实，`Summaries` 只显示且展开摘要。
- 搜索继续使用当前不区分大小写的 substring 语义；摘要命中不得隐藏在折叠区域。
- 保留导入、导出、新增、编辑、删除、来源跳转和清空全部记忆的现有行为与确认弹窗。
- 不新增排序、分页、批量操作、摘要编辑、分析图表或第三方依赖。
- 必须兼容浅色、深色、`390px` 窄屏和 512 字符无断点文本，不产生横向溢出。
- 所有行为先写失败测试，再写最小生产代码；每个任务独立提交。

---

### Task 1: 记忆视图模型与双语文案

**Files:**
- Create: `frontend/src/components/workspace/settings/memory/memory-view-model.ts`
- Create: `frontend/tests/unit/components/workspace/settings/memory/memory-view-model.test.ts`
- Modify: `frontend/src/components/workspace/settings/memory-settings-page.tsx`
- Modify: `frontend/src/core/i18n/locales/types.ts`
- Modify: `frontend/src/core/i18n/locales/en-US.ts`
- Modify: `frontend/src/core/i18n/locales/zh-CN.ts`

**Interfaces:**
- Produces: `MemoryViewFilter`, `MemoryFact`, `MemorySection`, `MemorySectionGroup`, `ConfidenceLevel`, `MemoryCategoryVisual`.
- Produces: `buildMemorySectionGroups(memory, t)`, `countPopulatedSummaries(groups)`, `isMemorySummaryEmpty(memory)`, `confidenceToLevelKey(confidence)`, `getMemoryCategoryVisual(category)`, `truncateFactPreview(content, maxLength?)`, `upperFirst(value)`.
- Consumes: `UserMemory` and `Translations` only; no hooks and no API calls.

- [ ] **Step 1: 写失败的视图模型单元测试**

创建 `frontend/tests/unit/components/workspace/settings/memory/memory-view-model.test.ts`：

```ts
import { describe, expect, it } from "@rstest/core";

import {
  buildMemorySectionGroups,
  confidenceToLevelKey,
  countPopulatedSummaries,
  getMemoryCategoryVisual,
  isMemorySummaryEmpty,
  truncateFactPreview,
  upperFirst,
} from "@/components/workspace/settings/memory/memory-view-model";
import { enUS } from "@/core/i18n/locales/en-US";
import type { UserMemory } from "@/core/memory/types";

const memory: UserMemory = {
  version: "1",
  lastUpdated: "2026-07-10T00:00:00Z",
  user: {
    workContext: {
      summary: "Prefers source-backed work.",
      updatedAt: "2026-07-10T00:00:00Z",
    },
    personalContext: { summary: "", updatedAt: "" },
    topOfMind: {
      summary: "Redesigning DeerFlow.",
      updatedAt: "2026-07-10T00:00:00Z",
    },
  },
  history: {
    recentMonths: { summary: "Forked DeerFlow.", updatedAt: "2026-07-10T00:00:00Z" },
    earlierContext: { summary: "", updatedAt: "" },
    longTermBackground: { summary: "", updatedAt: "" },
  },
  facts: [],
};

describe("memory view model", () => {
  it("builds the two existing summary groups and counts non-empty sections", () => {
    const groups = buildMemorySectionGroups(memory, enUS);

    expect(groups).toHaveLength(2);
    expect(groups.flatMap((group) => group.sections)).toHaveLength(6);
    expect(countPopulatedSummaries(groups)).toBe(3);
    expect(isMemorySummaryEmpty(memory)).toBe(false);
  });

  it("maps confidence values to the existing localized levels", () => {
    expect(confidenceToLevelKey(0.95)).toEqual({ key: "veryHigh", value: 0.95 });
    expect(confidenceToLevelKey(0.7)).toEqual({ key: "high", value: 0.7 });
    expect(confidenceToLevelKey(0.2)).toEqual({ key: "normal", value: 0.2 });
    expect(confidenceToLevelKey(Number.NaN)).toEqual({ key: "unknown" });
  });

  it("normalizes arbitrary category names to stable visual keys", () => {
    expect(getMemoryCategoryVisual("preference")).toBe("preference");
    expect(getMemoryCategoryVisual("PERSONAL")).toBe("preference");
    expect(getMemoryCategoryVisual("work")).toBe("work");
    expect(getMemoryCategoryVisual("project-context")).toBe("project");
    expect(getMemoryCategoryVisual("context")).toBe("context");
    expect(getMemoryCategoryVisual("anything-else")).toBe("default");
  });

  it("keeps preview and label formatting deterministic", () => {
    expect(truncateFactPreview("  a   b  ", 20)).toBe("a b");
    expect(truncateFactPreview("abcdefgh", 6)).toBe("abc...");
    expect(upperFirst("context")).toBe("Context");
  });
});
```

- [ ] **Step 2: 运行单元测试并确认模块尚不存在**

Run: `cd frontend && pnpm test -- tests/unit/components/workspace/settings/memory/memory-view-model.test.ts`

Expected: FAIL，报错包含 `Cannot find module .../memory-view-model`。

- [ ] **Step 3: 实现纯视图模型并从页面移除重复定义**

创建 `frontend/src/components/workspace/settings/memory/memory-view-model.ts`，实现以下完整导出；`buildMemorySectionGroups` 的六个字段与当前页面保持一一对应：

```ts
import type { Translations } from "@/core/i18n/locales/types";
import type { UserMemory } from "@/core/memory/types";

export type MemoryViewFilter = "all" | "facts" | "summaries";
export type MemoryFact = UserMemory["facts"][number];
export type ConfidenceLevel = "veryHigh" | "high" | "normal" | "unknown";
export type MemoryCategoryVisual =
  | "preference"
  | "work"
  | "project"
  | "context"
  | "default";

export type MemorySection = {
  title: string;
  summary: string;
  updatedAt?: string;
};

export type MemorySectionGroup = {
  title: string;
  sections: MemorySection[];
};

export function buildMemorySectionGroups(
  memory: UserMemory,
  t: Translations,
): MemorySectionGroup[] {
  return [
    {
      title: t.settings.memory.markdown.userContext,
      sections: [
        {
          title: t.settings.memory.markdown.work,
          summary: memory.user.workContext.summary,
          updatedAt: memory.user.workContext.updatedAt,
        },
        {
          title: t.settings.memory.markdown.personal,
          summary: memory.user.personalContext.summary,
          updatedAt: memory.user.personalContext.updatedAt,
        },
        {
          title: t.settings.memory.markdown.topOfMind,
          summary: memory.user.topOfMind.summary,
          updatedAt: memory.user.topOfMind.updatedAt,
        },
      ],
    },
    {
      title: t.settings.memory.markdown.historyBackground,
      sections: [
        {
          title: t.settings.memory.markdown.recentMonths,
          summary: memory.history.recentMonths.summary,
          updatedAt: memory.history.recentMonths.updatedAt,
        },
        {
          title: t.settings.memory.markdown.earlierContext,
          summary: memory.history.earlierContext.summary,
          updatedAt: memory.history.earlierContext.updatedAt,
        },
        {
          title: t.settings.memory.markdown.longTermBackground,
          summary: memory.history.longTermBackground.summary,
          updatedAt: memory.history.longTermBackground.updatedAt,
        },
      ],
    },
  ];
}

export function countPopulatedSummaries(groups: MemorySectionGroup[]) {
  return groups.reduce(
    (count, group) =>
      count + group.sections.filter((section) => section.summary.trim()).length,
    0,
  );
}

export function isMemorySummaryEmpty(memory: UserMemory) {
  return [
    memory.user.workContext.summary,
    memory.user.personalContext.summary,
    memory.user.topOfMind.summary,
    memory.history.recentMonths.summary,
    memory.history.earlierContext.summary,
    memory.history.longTermBackground.summary,
  ].every((summary) => summary.trim() === "");
}

export function confidenceToLevelKey(confidence: unknown): {
  key: ConfidenceLevel;
  value?: number;
} {
  if (typeof confidence !== "number" || !Number.isFinite(confidence)) {
    return { key: "unknown" };
  }
  const value = Math.min(1, Math.max(0, confidence));
  if (value >= 0.85) return { key: "veryHigh", value };
  if (value >= 0.65) return { key: "high", value };
  return { key: "normal", value };
}

export function getMemoryCategoryVisual(category: string): MemoryCategoryVisual {
  const normalized = category.trim().toLowerCase();
  if (normalized.includes("preference") || normalized.includes("personal")) {
    return "preference";
  }
  if (normalized.includes("work")) return "work";
  if (normalized.includes("project")) return "project";
  if (normalized.includes("context")) return "context";
  return "default";
}

export function truncateFactPreview(content: string, maxLength = 140) {
  const normalized = content.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) return normalized;
  const ellipsis = "...";
  if (maxLength <= ellipsis.length) return normalized.slice(0, maxLength);
  return `${normalized.slice(0, maxLength - ellipsis.length)}${ellipsis}`;
}

export function upperFirst(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
```

在 `memory-settings-page.tsx` 中导入这些导出并删除原本同名的本地类型与函数。

- [ ] **Step 4: 增加记忆收件箱文案契约**

在 `Translations["settings"]["memory"]`、`en-US.ts` 和 `zh-CN.ts` 同步增加：

```ts
manageMemory: string;
factCount: (count: number) => string;
summaryCount: (count: number) => string;
recentFocus: string;
viewSummaries: string;
hideSummaries: string;
smartSummaries: string;
emptyTitle: string;
emptyDescription: string;
loadErrorTitle: string;
```

英文值：

```ts
description: "Review and manage what DeerFlow remembers about you.",
manageMemory: "Manage memory",
factCount: (count) => `${count} ${count === 1 ? "fact" : "facts"}`,
summaryCount: (count) => `${count} ${count === 1 ? "summary" : "summaries"}`,
recentFocus: "Recent focus",
viewSummaries: "View summaries",
hideSummaries: "Hide summaries",
smartSummaries: "Smart summaries",
emptyTitle: "No memory yet",
emptyDescription: "Add a fact or keep chatting so DeerFlow can learn useful context.",
loadErrorTitle: "Memory could not be loaded",
```

中文值：

```ts
description: "查看和管理 DeerFlow 对你的长期理解。",
manageMemory: "管理记忆",
factCount: (count) => `${count} 条事实`,
summaryCount: (count) => `${count} 个摘要`,
recentFocus: "近期关注",
viewSummaries: "查看摘要",
hideSummaries: "收起摘要",
smartSummaries: "智能摘要",
emptyTitle: "还没有记忆",
emptyDescription: "添加一条事实，或继续对话，让 DeerFlow 学习有用的上下文。",
loadErrorTitle: "无法加载记忆",
```

- [ ] **Step 5: 运行单元测试与类型检查**

Run:

```bash
cd frontend
pnpm test -- tests/unit/components/workspace/settings/memory/memory-view-model.test.ts
pnpm typecheck
```

Expected: 视图模型测试全部 PASS，TypeScript 退出码 0。

- [ ] **Step 6: 提交视图模型与文案**

```bash
git add frontend/src/components/workspace/settings/memory/memory-view-model.ts frontend/tests/unit/components/workspace/settings/memory/memory-view-model.test.ts frontend/src/components/workspace/settings/memory-settings-page.tsx frontend/src/core/i18n/locales/types.ts frontend/src/core/i18n/locales/en-US.ts frontend/src/core/i18n/locales/zh-CN.ts
git commit -m "refactor(frontend): extract memory inbox view model"
```

---

### Task 2: 页面头部、管理菜单、概览与工具栏

**Files:**
- Create: `frontend/src/components/workspace/settings/memory/memory-workbench.tsx`
- Modify: `frontend/src/components/workspace/settings/memory-settings-page.tsx`
- Modify: `frontend/tests/e2e/memory-workbench.spec.ts`

**Interfaces:**
- Produces: `MemoryHeaderActions`, `MemoryOverview`, `MemoryToolbar`.
- `MemoryHeaderActions` consumes callbacks `onAddFact`, `onImport`, `onExport`, `onClear` plus pending flags; it never owns mutations.
- `MemoryOverview` consumes `factCount`, `summaryCount`, `lastUpdated`, `recentFocus`, and `onViewSummaries`.
- `MemoryToolbar` consumes controlled `query`, `filter`, `onQueryChange`, and `onFilterChange`.

- [ ] **Step 1: 先更新头部与管理菜单 E2E**

将现有按钮层级测试替换为：

```ts
test("memory inbox exposes primary add and secondary management actions", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await expect(page.getByRole("button", { name: "Add fact" })).toHaveAttribute(
    "data-variant",
    "default",
  );
  const manage = page.getByRole("button", { name: "Manage memory" });
  await expect(manage).toHaveAttribute("data-variant", "outline");

  await manage.click();
  await expect(page.getByRole("menuitem", { name: "Import memory" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Export memory" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Clear all memory" })).toHaveAttribute(
    "data-variant",
    "destructive",
  );
});

test("memory overview derives counts and recent focus from loaded memory", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  const overview = page.getByTestId("memory-overview");
  await expect(overview).toContainText("1 fact");
  await expect(overview).toContainText("4 summaries");
  await expect(overview).toContainText("Redesigning DeerFlow.");
  await expect(overview.getByRole("button", { name: "View summaries" })).toBeVisible();
});
```

Fixture 中非空摘要为 work、personal、topOfMind、recentMonths，因此期望值是 `4 summaries`。

- [ ] **Step 2: 运行 E2E 并确认新入口不存在**

Run: `cd frontend && pnpm test:e2e tests/e2e/memory-workbench.spec.ts --grep "memory inbox|memory overview"`

Expected: FAIL，找不到 `Manage memory` 或 `memory-overview`。

- [ ] **Step 3: 创建三个纯展示组件**

在 `memory-workbench.tsx` 中使用现有 `Button`、`Input`、`ToggleGroup`、`DropdownMenu` 和 Lucide 图标。组件签名固定为：

```ts
export function MemoryHeaderActions(props: {
  t: Translations;
  isImporting: boolean;
  isExporting: boolean;
  isClearing: boolean;
  onAddFact: () => void;
  onImport: () => void;
  onExport: () => void;
  onClear: () => void;
}): React.ReactNode;

export function MemoryOverview(props: {
  t: Translations;
  factCount: number;
  summaryCount: number;
  lastUpdated: string;
  recentFocus: string;
  onViewSummaries: () => void;
}): React.ReactNode;

export function MemoryToolbar(props: {
  t: Translations;
  query: string;
  filter: MemoryViewFilter;
  onQueryChange: (query: string) => void;
  onFilterChange: (filter: MemoryViewFilter) => void;
}): React.ReactNode;
```

`MemoryHeaderActions` 的关键结构必须是：

```tsx
<div className="flex flex-wrap items-center gap-2">
  <DropdownMenu>
    <DropdownMenuTrigger asChild>
      <Button variant="outline">
        <Settings2Icon aria-hidden="true" />
        {t.settings.memory.manageMemory}
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="end" className="w-52">
      <DropdownMenuItem disabled={isImporting} onSelect={onImport}>
        <UploadIcon aria-hidden="true" />
        {t.settings.memory.importButton}
      </DropdownMenuItem>
      <DropdownMenuItem disabled={isExporting} onSelect={onExport}>
        <DownloadIcon aria-hidden="true" />
        {isExporting ? t.common.loading : t.settings.memory.exportButton}
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      <DropdownMenuItem
        variant="destructive"
        disabled={isClearing}
        onSelect={onClear}
      >
        <Trash2Icon aria-hidden="true" />
        {isClearing ? t.common.loading : t.settings.memory.clearAll}
      </DropdownMenuItem>
    </DropdownMenuContent>
  </DropdownMenu>
  <Button onClick={onAddFact}>
    <PlusIcon aria-hidden="true" />
    {t.settings.memory.addFact}
  </Button>
</div>
```

`MemoryOverview` 使用一个 `rounded-xl border bg-card` 容器，内部在桌面为四列，在小屏为两列；事实数、摘要数、更新时间使用 `FileTextIcon`、`Layers3Icon`、`Clock3Icon`，近期关注使用 `StarIcon` 并占剩余宽度。所有图标 `aria-hidden="true"`。外层添加 `data-testid="memory-overview"`。

`MemoryToolbar` 在搜索框内使用绝对定位的 `SearchIcon` 和 `pl-9`，Filter 继续保留三个既有 test id。窄屏用 `flex-col`，`sm` 以上为一行。

- [ ] **Step 4: 在 controller 中接入新头部、概览和工具栏**

保留 hidden file input 在 `MemorySettingsPage`，将头部动作回调接到既有函数：

```tsx
<input
  ref={fileInputRef}
  type="file"
  accept=".json,application/json"
  className="hidden"
  onChange={(event) => void handleImportFileSelection(event)}
/>
<MemoryHeaderActions
  t={t}
  isImporting={importMemoryMutation.isPending}
  isExporting={isExporting}
  isClearing={clearMemory.isPending}
  onAddFact={openCreateFactDialog}
  onImport={() => fileInputRef.current?.click()}
  onExport={() => void handleExportMemory()}
  onClear={() => setClearDialogOpen(true)}
/>
```

在 loaded-memory 分支中计算：

```ts
const summaryCount = countPopulatedSummaries(sectionGroups);
const recentFocus =
  memory?.user.topOfMind.summary.trim() || t.settings.memory.markdown.empty;
```

然后渲染：

```tsx
<MemoryOverview
  t={t}
  factCount={memory.facts.length}
  summaryCount={summaryCount}
  lastUpdated={formatTimeAgo(memory.lastUpdated)}
  recentFocus={recentFocus}
  onViewSummaries={() => setFilter("summaries")}
/>
<MemoryToolbar
  t={t}
  query={query}
  filter={filter}
  onQueryChange={setQuery}
  onFilterChange={setFilter}
/>
```

本任务暂时允许 `View summaries` 切换到 summaries filter；Task 3 将它改成展开并聚焦 disclosure。导入、导出、清空和新增函数本体不得改写。

- [ ] **Step 5: 运行目标 E2E 和静态检查**

Run:

```bash
cd frontend
pnpm test:e2e tests/e2e/memory-workbench.spec.ts --grep "memory inbox|memory overview"
pnpm check
```

Expected: 两个目标 E2E PASS，ESLint 与 TypeScript 退出码 0。

- [ ] **Step 6: 提交页面外壳**

```bash
git add frontend/src/components/workspace/settings/memory/memory-workbench.tsx frontend/src/components/workspace/settings/memory-settings-page.tsx frontend/tests/e2e/memory-workbench.spec.ts
git commit -m "feat(frontend): add memory inbox shell"
```

---

### Task 3: 全宽事实列表、摘要 disclosure 与状态页面

**Files:**
- Modify: `frontend/src/components/workspace/settings/memory/memory-workbench.tsx`
- Modify: `frontend/src/components/workspace/settings/memory-settings-page.tsx`
- Modify: `frontend/tests/e2e/memory-workbench.spec.ts`

**Interfaces:**
- Produces: `MemoryFactList`, `MemorySummaryDisclosure`, `MemoryEmptyState`, `MemoryLoadingState`, `MemoryLoadError`.
- `MemoryFactList` consumes already-filtered facts and edit/delete callbacks.
- `MemorySummaryDisclosure` is controlled by `open` and `onOpenChange`; it receives already-filtered groups and a forwarded trigger ref.
- Preserves: existing `memory-workbench`, `memory-facts-panel`, `memory-summary-panel`, and filter test ids.

- [ ] **Step 1: 将布局、筛选和折叠行为写成失败 E2E**

用以下行为替换旧的 `36/64` 测试，并保留现有窄屏长文本 fixture：

```ts
test("facts stay full width while summaries use a disclosure", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  const workbench = page.getByTestId("memory-workbench");
  const facts = page.getByTestId("memory-facts-panel");
  const disclosure = page.getByTestId("memory-summary-disclosure");
  await expect(facts).toBeVisible();
  await expect(disclosure).toBeVisible();
  await expect(page.getByTestId("memory-summary-panel")).toHaveCount(0);

  const workbenchBox = await workbench.boundingBox();
  const factsBox = await facts.boundingBox();
  expect(workbenchBox).not.toBeNull();
  expect(factsBox).not.toBeNull();
  expect(factsBox!.width / workbenchBox!.width).toBeGreaterThan(0.95);

  await disclosure.getByRole("button", { name: /Smart summaries/ }).click();
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
});

test("filters preserve facts and summaries visibility", async ({ page }) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await page.getByTestId("memory-filter-facts").click();
  await expect(page.getByTestId("memory-facts-panel")).toBeVisible();
  await expect(page.getByTestId("memory-summary-disclosure")).toHaveCount(0);

  await page.getByTestId("memory-filter-summaries").click();
  await expect(page.getByTestId("memory-facts-panel")).toHaveCount(0);
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
});

test("a summary-only search match opens the matching summary", async ({ page }) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await page.getByPlaceholder("Search memory").fill("Forked DeerFlow");
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
  await expect(page.getByText("Forked DeerFlow.", { exact: true })).toBeVisible();
  await expect(page.getByTestId("memory-facts-panel")).toHaveCount(0);
});
```

增加 fully empty fixture，并验证恢复入口：

```ts
test("fully empty memory renders one recovery state without empty panels", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page, emptyMemoryFixture);
  await page.goto("/workspace/memory");

  await expect(page.getByTestId("memory-empty-state")).toBeVisible();
  await expect(page.getByTestId("memory-facts-panel")).toHaveCount(0);
  await expect(page.getByTestId("memory-summary-panel")).toHaveCount(0);
  await expect(
    page.getByTestId("memory-empty-state").getByRole("button", { name: "Add fact" }),
  ).toBeVisible();
});
```

- [ ] **Step 2: 运行新 E2E 并确认旧分栏和常显摘要导致失败**

Run: `cd frontend && pnpm test:e2e tests/e2e/memory-workbench.spec.ts --grep "full width|filters preserve|summary-only|fully empty"`

Expected: FAIL，原因至少包含 disclosure test id 不存在或摘要仍常显。

- [ ] **Step 3: 实现事实列表**

`MemoryFactList` 外层固定为：

```tsx
<section
  data-testid="memory-facts-panel"
  aria-labelledby="memory-facts-heading"
  className="bg-card min-w-0 overflow-hidden rounded-xl border"
>
  <div className="flex items-center justify-between gap-3 px-4 py-3 sm:px-5">
    <h2 id="memory-facts-heading" className="text-sm font-medium">
      {t.settings.memory.markdown.facts}
    </h2>
    <span className="text-muted-foreground text-xs">
      {t.settings.memory.factCount(facts.length)}
    </span>
  </div>
  <div className="divide-y">
    {facts.map((fact) => (
      <MemoryFactRow
        key={fact.id}
        fact={fact}
        t={t}
        onEdit={() => onEdit(fact)}
        onDelete={() => onDelete(fact)}
        disabled={isDeleting}
      />
    ))}
  </div>
</section>
```

`MemoryFactRow` 使用 `getMemoryCategoryVisual` 将 preference/personal 映射到 `HeartIcon`、work 到 `BriefcaseBusinessIcon`、project 到 `FolderGit2Icon`、context 到 `NotebookTextIcon`、default 到 `BrainIcon`。事实原始 `content` 是唯一主文本。类别、置信度、创建时间和来源放入 `flex flex-wrap gap-x-3 gap-y-1` 元数据行；非 manual 来源继续使用：

```tsx
<Link
  href={pathOfThread(fact.source)}
  className="text-primary font-medium underline-offset-4 hover:underline"
>
  {t.settings.memory.markdown.table.view}
</Link>
```

行容器必须包含 `min-w-0 [overflow-wrap:anywhere]`，操作按钮使用现有 `aria-label={t.common.edit}` 与 `aria-label={t.common.delete}`。

- [ ] **Step 4: 实现受控摘要 disclosure**

使用现有 `Collapsible` 组件，结构固定为：

```tsx
<Collapsible
  open={open}
  onOpenChange={onOpenChange}
  data-testid="memory-summary-disclosure"
  className="bg-card min-w-0 overflow-hidden rounded-xl border"
>
  <CollapsibleTrigger asChild>
    <Button
      ref={triggerRef}
      variant="ghost"
      className="h-auto w-full justify-between rounded-none px-4 py-4 sm:px-5"
      aria-controls="memory-summary-content"
    >
      <span className="flex min-w-0 items-center gap-3 text-left">
        <SparklesIcon aria-hidden="true" className="text-muted-foreground size-4" />
        <span>
          <span className="block text-sm font-medium">
            {t.settings.memory.smartSummaries}
          </span>
          <span className="text-muted-foreground block text-xs">
            {t.settings.memory.summaryCount(summaryCount)} · {t.settings.memory.summaryReadOnly}
          </span>
        </span>
      </span>
      <ChevronDownIcon
        aria-hidden="true"
        className={cn("size-4 transition-transform", open && "rotate-180")}
      />
    </Button>
  </CollapsibleTrigger>
  <CollapsibleContent id="memory-summary-content">
    <div data-testid="memory-summary-panel" className="border-t p-4 sm:p-5">
      <div className="grid gap-6 lg:grid-cols-2">
        {groups.map((group) => (
          <section key={group.title} className="min-w-0 space-y-4">
            <h3 className="text-sm font-medium">{group.title}</h3>
            {group.sections.map((section) => (
              <div key={section.title} className="min-w-0 border-t pt-3 first:border-t-0 first:pt-0">
                <h4 className="text-sm font-medium">{section.title}</h4>
                <SafeStreamdown
                  className="mt-2 min-w-0 text-sm [overflow-wrap:anywhere] [&>*:first-child]:mt-0 [&>*:last-child]:mb-0"
                  {...streamdownPlugins}
                >
                  {section.summary.trim() || t.settings.memory.markdown.empty}
                </SafeStreamdown>
                {section.updatedAt ? (
                  <p className="text-muted-foreground mt-2 text-xs">
                    {t.settings.memory.markdown.updatedAt}: {formatTimeAgo(section.updatedAt)}
                  </p>
                ) : null}
              </div>
            ))}
          </section>
        ))}
      </div>
    </div>
  </CollapsibleContent>
</Collapsible>
```

`triggerRef` 类型为 `React.Ref<HTMLButtonElement>`。展示组件只调用 `onOpenChange`，不自行决定强制展开规则。

- [ ] **Step 5: 在 controller 中实现显示和聚焦规则**

增加：

```ts
const [summariesExpanded, setSummariesExpanded] = useState(false);
const summaryTriggerRef = useRef<HTMLButtonElement | null>(null);

const hasSummarySearchMatch =
  normalizedQuery.length > 0 && filteredSectionGroups.length > 0;
const summariesForcedOpen = filter === "summaries" || hasSummarySearchMatch;
const summariesOpen = summariesForcedOpen || summariesExpanded;
const showFacts = filter !== "summaries";
const showSummaries = filter !== "facts";

function handleViewSummaries() {
  setSummariesExpanded(true);
  requestAnimationFrame(() => {
    summaryTriggerRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    summaryTriggerRef.current?.focus({ preventScroll: true });
  });
}
```

`MemoryOverview.onViewSummaries` 改为 `handleViewSummaries`。`MemorySummaryDisclosure.onOpenChange` 仅在 `!summariesForcedOpen` 时更新 `setSummariesExpanded`。

内容分支顺序固定为：无 memory 的现有 empty、fully empty、no matches、facts、summary disclosure。Fully empty 时渲染 `MemoryEmptyState`，其按钮调用 `openCreateFactDialog`。Facts 为空但不是 fully empty 时，`MemoryFactList` 显示现有 `noFacts` 文案，不创建固定高度空面板。

删除旧的 `summariesToMarkdown`、`formatMemorySection`、两列 `cn(...lg:grid-cols...)` 和逐事实 card JSX。

- [ ] **Step 6: 实现 loading 与 error 组件**

`MemoryLoadingState` 使用 `Skeleton` 组合出一个概览条、一个工具栏和三个列表行，外层 `data-testid="memory-loading-state"`。`MemoryLoadError` 使用 `Alert variant="destructive"`、`AlertCircleIcon`、`loadErrorTitle` 和实际 `error.message`。不增加 retry 按钮。

- [ ] **Step 7: 运行本任务 E2E、单元测试和静态检查**

Run:

```bash
cd frontend
pnpm test:e2e tests/e2e/memory-workbench.spec.ts
pnpm test -- tests/unit/components/workspace/settings/memory/memory-view-model.test.ts
pnpm check
```

Expected: memory E2E 全部 PASS，目标单元测试 PASS，ESLint 与 TypeScript 退出码 0。

- [ ] **Step 8: 提交事实优先内容区**

```bash
git add frontend/src/components/workspace/settings/memory/memory-workbench.tsx frontend/src/components/workspace/settings/memory-settings-page.tsx frontend/tests/e2e/memory-workbench.spec.ts
git commit -m "feat(frontend): build fact-first memory inbox"
```

---

### Task 4: 核心操作回归、响应式验收与文档同步

**Files:**
- Modify: `frontend/tests/e2e/memory-workbench.spec.ts`
- Modify: `frontend/AGENTS.md`

**Interfaces:**
- Produces: stateful Playwright memory API mock covering all existing endpoints.
- Verifies: POST fact、PATCH fact、DELETE fact、GET export、POST import、DELETE memory、thread source link.
- Preserves: current request JSON and downloaded export filename prefix.

- [ ] **Step 1: 将 E2E memory mock 升级为可变 API**

用一个正则 route 同时接管 exact memory 与子路径：

```ts
type MemoryFixture = typeof memoryFixture;
type MemoryRequest = {
  method: string;
  path: string;
  body: unknown;
};

async function mockMemoryAPI(page: Page, initial: MemoryFixture = memoryFixture) {
  let current = structuredClone(initial);
  const requests: MemoryRequest[] = [];

  await page.route(/\/api\/memory(?:\/.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const body = request.postDataJSON() as unknown;
    requests.push({ method, path: url.pathname, body });

    if (method === "GET" && url.pathname.endsWith("/api/memory/export")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) });
      return;
    }
    if (method === "POST" && url.pathname.endsWith("/api/memory/import")) {
      current = structuredClone(body as MemoryFixture);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) });
      return;
    }
    if (method === "POST" && url.pathname.endsWith("/api/memory/facts")) {
      const input = body as { content: string; category: string; confidence: number };
      current = {
        ...current,
        facts: [
          ...current.facts,
          {
            id: "fact-created",
            ...input,
            createdAt: "2026-07-10T01:00:00Z",
            source: "manual",
          },
        ],
      };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) });
      return;
    }
    const factMatch = url.pathname.match(/\/api\/memory\/facts\/([^/]+)$/);
    if (factMatch && method === "PATCH") {
      current = {
        ...current,
        facts: current.facts.map((fact) =>
          fact.id === decodeURIComponent(factMatch[1]!)
            ? { ...fact, ...(body as object) }
            : fact,
        ),
      };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) });
      return;
    }
    if (factMatch && method === "DELETE") {
      current = {
        ...current,
        facts: current.facts.filter(
          (fact) => fact.id !== decodeURIComponent(factMatch[1]!),
        ),
      };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) });
      return;
    }
    if (method === "DELETE" && url.pathname.endsWith("/api/memory")) {
      current = structuredClone(emptyMemoryFixture);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) });
  });

  return { requests };
}
```

所有调用点改为 `await mockMemoryAPI(page)`，避免 route 注册完成前导航。

- [ ] **Step 2: 先写新增、编辑和删除事实回归 E2E**

```ts
test("fact create, edit, and delete keep their existing request contracts", async ({ page }) => {
  mockLangGraphAPI(page);
  const api = await mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await page.getByRole("button", { name: "Add fact" }).click();
  await page.getByLabel("Content").fill("New durable context");
  await page.getByLabel("Category").fill("context");
  await page.getByLabel("Confidence").fill("0.75");
  await page.getByRole("button", { name: "Save fact" }).click();
  await expect(page.getByText("New durable context", { exact: true })).toBeVisible();

  const created = api.requests.find(
    (request) => request.method === "POST" && request.path.endsWith("/api/memory/facts"),
  );
  expect(created?.body).toEqual({
    content: "New durable context",
    category: "context",
    confidence: 0.75,
  });

  const createdRow = page.getByTestId("memory-fact-row-fact-created");
  await createdRow.getByRole("button", { name: "Edit" }).click();
  await page.getByLabel("Content").fill("Updated durable context");
  await page.getByRole("button", { name: "Save fact" }).click();
  await expect(page.getByText("Updated durable context", { exact: true })).toBeVisible();
  expect(api.requests.some((request) => request.method === "PATCH" && request.path.endsWith("/api/memory/facts/fact-created"))).toBe(true);

  const updatedRow = page.getByTestId("memory-fact-row-fact-created");
  await updatedRow.getByRole("button", { name: "Delete" }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByText("Updated durable context", { exact: true })).toHaveCount(0);
  expect(api.requests.some((request) => request.method === "DELETE" && request.path.endsWith("/api/memory/facts/fact-created"))).toBe(true);
});
```

先运行该测试并确认因 fact row test id 不存在而失败，然后在 `MemoryFactRow` 最外层增加：

```tsx
data-testid={`memory-fact-row-${fact.id}`}
```

再次运行并确认 create、PATCH 和 DELETE 断言全部通过。

- [ ] **Step 3: 先写导入、导出和清空回归 E2E**

```ts
test("management menu preserves export, import, and clear flows", async ({ page }) => {
  mockLangGraphAPI(page);
  const api = await mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await page.getByRole("button", { name: "Manage memory" }).click();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("menuitem", { name: "Export memory" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^deerflow-memory-.*\.json$/);
  expect(api.requests.some((request) => request.method === "GET" && request.path.endsWith("/api/memory/export"))).toBe(true);

  await page.getByRole("button", { name: "Manage memory" }).click();
  const chooserPromise = page.waitForEvent("filechooser");
  await page.getByRole("menuitem", { name: "Import memory" }).click();
  const chooser = await chooserPromise;
  await chooser.setFiles({
    name: "memory.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(memoryFixture)),
  });
  await page.getByRole("button", { name: "Import", exact: true }).click();
  expect(api.requests.some((request) => request.method === "POST" && request.path.endsWith("/api/memory/import"))).toBe(true);

  await page.getByRole("button", { name: "Manage memory" }).click();
  await page.getByRole("menuitem", { name: "Clear all memory" }).click();
  await page.getByRole("button", { name: "Clear all memory" }).click();
  await expect(page.getByTestId("memory-empty-state")).toBeVisible();
  expect(api.requests.some((request) => request.method === "DELETE" && request.path.endsWith("/api/memory"))).toBe(true);
});
```

- [ ] **Step 4: 验证 learned source、窄屏和深色模式**

保留当前 `expectNoHorizontalOverflow` 与 `expectPageNoHorizontalOverflow` helper，增加一条 source test：fixture 的 fact source 使用 `MOCK_THREAD_ID`，点击 `View` 后断言 URL 是 `pathOfThread` 对应的 `/workspace/chats/${MOCK_THREAD_ID}`。

保留 `390 x 844` 长文本测试和 persisted dark theme 测试，但更新断言：summary disclosure 可见、facts panel 可见、展开摘要后 summary panel 可见，并且三者及 document 均无横向溢出。

- [ ] **Step 5: 运行全部 memory E2E 并修复仅由重构引起的失败**

Run: `cd frontend && pnpm test:e2e tests/e2e/memory-workbench.spec.ts`

Expected: 所有 memory tests PASS；请求断言分别命中原有 endpoint 和 method。

- [ ] **Step 6: 同步架构文档**

在 `frontend/AGENTS.md` 的 Interaction Ownership 将现有 memory 条目更新为：

```md
- `src/components/workspace/settings/memory-settings-page.tsx` owns memory queries, filters, file import/export, dialogs, and mutations; components under `settings/memory/` are presentation and pure view-model helpers only, and must not call memory APIs or change mutation payloads.
```

- [ ] **Step 7: 执行完整前端验证**

Run:

```bash
cd frontend
pnpm test
pnpm test:e2e tests/e2e/memory-workbench.spec.ts
pnpm check
pnpm format
```

Expected: 单元测试 0 失败，memory E2E 0 失败，ESLint/TypeScript 退出码 0，Prettier 退出码 0。

- [ ] **Step 8: 浏览器验收**

在本地工作区检查以下固定矩阵：

1. `1440 x 900` loaded memory：事实全宽、摘要默认折叠、管理菜单可达。
2. `390 x 844` loaded memory：所有行换行且无横向滚动。
3. light 与 dark：正文、元数据、边框、primary/destructive 动作对比清晰。
4. fully empty、facts-only、summaries-only、no-match、load error：各状态只有一个清晰主信息。
5. 新增、编辑、删除、导入、导出、清空确认弹窗和 learned source 跳转均可完成。

- [ ] **Step 9: 提交回归覆盖和文档**

```bash
git add frontend/tests/e2e/memory-workbench.spec.ts frontend/src/components/workspace/settings/memory/memory-workbench.tsx frontend/AGENTS.md
git commit -m "test(frontend): lock memory inbox behavior"
git status --short
```

Expected: 本功能修改全部已提交，工作树没有本功能遗留文件。
