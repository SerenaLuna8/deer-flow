# Workspace Capability Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将记忆、工具、技能迁移为工作区左侧主导航中的三个独立页面，并从设置弹窗移除对应菜单。

**Architecture:** 使用一个纯数据导航契约驱动左侧三个入口，便于验证顺序、链接和激活逻辑；三个 App Router 页面通过统一的工作区内容容器复用现有管理组件。设置弹窗保留原有结构，但其 section 契约缩减为账号、外观、通知、频道和关于。

**Tech Stack:** Next.js 16 App Router、React 19、TypeScript 5.8、Tailwind CSS 4、Rstest、pnpm。

## Global Constraints

- 路由固定为 `/workspace/memory`、`/workspace/tools`、`/workspace/skills`。
- 左侧顺序固定为“记忆 → 工具 → 技能”，位于“定时任务”之后。
- 保留现有记忆、MCP 工具和技能组件的业务行为及权限判断。
- 设置弹窗不再包含 `memory`、`tools`、`skills` section。
- 不移动或重命名现有三个管理组件，不修改后端接口和权限模型。
- 生产代码必须在对应失败测试之后编写。

---

### Task 1: 工作区能力导航契约与左侧入口

**Files:**
- Create: `frontend/src/components/workspace/workspace-capability-links.ts`
- Modify: `frontend/src/components/workspace/workspace-nav-chat-list.tsx`
- Test: `frontend/tests/unit/components/workspace/workspace-capability-links.test.ts`

**Interfaces:**
- Produces: `WORKSPACE_CAPABILITY_LINKS`，元素类型为 `{ id: "memory" | "tools" | "skills"; href: string }`。
- Produces: `isWorkspaceCapabilityPath(pathname: string, href: string): boolean`。
- Consumes: 现有 `t.settings.sections` 文案、`SidebarMenuButton` 和 `usePathname()`。

- [ ] **Step 1: 写导航契约失败测试**

```ts
import { describe, expect, it } from "@rstest/core";

import {
  WORKSPACE_CAPABILITY_LINKS,
  isWorkspaceCapabilityPath,
} from "@/components/workspace/workspace-capability-links";

describe("WORKSPACE_CAPABILITY_LINKS", () => {
  it("keeps memory, tools, and skills in the requested order", () => {
    expect(WORKSPACE_CAPABILITY_LINKS).toEqual([
      { id: "memory", href: "/workspace/memory" },
      { id: "tools", href: "/workspace/tools" },
      { id: "skills", href: "/workspace/skills" },
    ]);
  });
});

describe("isWorkspaceCapabilityPath", () => {
  it("matches a capability root and its descendants only", () => {
    expect(isWorkspaceCapabilityPath("/workspace/memory", "/workspace/memory")).toBe(true);
    expect(isWorkspaceCapabilityPath("/workspace/memory/fact", "/workspace/memory")).toBe(true);
    expect(isWorkspaceCapabilityPath("/workspace/memory-bank", "/workspace/memory")).toBe(false);
  });
});
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `cd frontend && pnpm test tests/unit/components/workspace/workspace-capability-links.test.ts`

Expected: FAIL，提示无法解析 `workspace-capability-links`。

- [ ] **Step 3: 实现纯导航契约**

```ts
export const WORKSPACE_CAPABILITY_LINKS = [
  { id: "memory", href: "/workspace/memory" },
  { id: "tools", href: "/workspace/tools" },
  { id: "skills", href: "/workspace/skills" },
] as const;

export function isWorkspaceCapabilityPath(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}
```

- [ ] **Step 4: 在定时任务后渲染三个左侧入口**

在 `workspace-nav-chat-list.tsx` 中导入 `BrainIcon`、`WrenchIcon`、`SparklesIcon` 和导航契约，声明图标映射并在定时任务的 `SidebarMenuItem` 后追加：

```tsx
const capabilityIcons = {
  memory: BrainIcon,
  tools: WrenchIcon,
  skills: SparklesIcon,
} as const;

{WORKSPACE_CAPABILITY_LINKS.map(({ id, href }) => {
  const Icon = capabilityIcons[id];
  return (
    <SidebarMenuItem key={id}>
      <SidebarMenuButton
        isActive={isWorkspaceCapabilityPath(pathname, href)}
        asChild
      >
        <Link className="text-muted-foreground" href={href}>
          <Icon />
          <span>{t.settings.sections[id]}</span>
        </Link>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
})}
```

- [ ] **Step 5: 运行导航测试并确认通过**

Run: `cd frontend && pnpm test tests/unit/components/workspace/workspace-capability-links.test.ts`

Expected: PASS。

- [ ] **Step 6: 提交导航变更**

```bash
git add frontend/src/components/workspace/workspace-capability-links.ts frontend/src/components/workspace/workspace-nav-chat-list.tsx frontend/tests/unit/components/workspace/workspace-capability-links.test.ts
git commit -m "feat(frontend): add capability links to workspace navigation"
```

---

### Task 2: 从设置弹窗移除记忆、工具和技能

**Files:**
- Create: `frontend/src/components/workspace/settings/settings-sections.ts`
- Modify: `frontend/src/components/workspace/settings/settings-dialog.tsx`
- Modify: `frontend/src/components/workspace/workspace-nav-menu.tsx`
- Test: `frontend/tests/unit/components/workspace/settings/settings-sections.test.ts`

**Interfaces:**
- Produces: `SETTINGS_SECTION_IDS`，值固定为 `account`、`appearance`、`notification`、`channels`、`about`。
- Produces: `SettingsSectionId` 联合类型。
- Consumes: `SettingsDialog.defaultSection` 与“设置”“关于”两个现有打开入口。

- [ ] **Step 1: 写设置 section 失败测试**

```ts
import { describe, expect, it } from "@rstest/core";

import { SETTINGS_SECTION_IDS } from "@/components/workspace/settings/settings-sections";

describe("SETTINGS_SECTION_IDS", () => {
  it("excludes capabilities promoted to workspace navigation", () => {
    expect(SETTINGS_SECTION_IDS).toEqual([
      "account",
      "appearance",
      "notification",
      "channels",
      "about",
    ]);
    expect(SETTINGS_SECTION_IDS).not.toContain("memory");
    expect(SETTINGS_SECTION_IDS).not.toContain("tools");
    expect(SETTINGS_SECTION_IDS).not.toContain("skills");
  });
});
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `cd frontend && pnpm test tests/unit/components/workspace/settings/settings-sections.test.ts`

Expected: FAIL，提示无法解析 `settings-sections`。

- [ ] **Step 3: 实现设置 section 契约**

```ts
export const SETTINGS_SECTION_IDS = [
  "account",
  "appearance",
  "notification",
  "channels",
  "about",
] as const;

export type SettingsSectionId = (typeof SETTINGS_SECTION_IDS)[number];
```

- [ ] **Step 4: 让设置弹窗由缩减后的契约驱动**

在 `settings-dialog.tsx` 中：

- 删除 `BrainIcon`、`SparklesIcon`、`WrenchIcon` 导入。
- 删除三个管理页面组件导入。
- 用 `SettingsSectionId` 替换本地 `SettingsSection` 类型。
- 使用 `SETTINGS_SECTION_IDS` 和下列图标映射生成菜单，标签读取 `t.settings.sections[id]`。

```tsx
const settingsSectionIcons = {
  account: UserIcon,
  appearance: PaletteIcon,
  notification: BellIcon,
  channels: CableIcon,
  about: InfoIcon,
} as const;
```

删除 `activeSection === "memory"`、`"tools"`、`"skills"` 三个内容分支，仅保留账号、外观、通知、频道和关于。

- [ ] **Step 5: 收窄“设置与更多”的默认 section 类型**

在 `workspace-nav-menu.tsx` 中把 `settingsDefaultSection` 类型改为：

```ts
useState<"appearance" | "about">("appearance");
```

保留“设置”打开 `appearance`、“关于”打开 `about` 的行为。

- [ ] **Step 6: 运行设置 section 测试并确认通过**

Run: `cd frontend && pnpm test tests/unit/components/workspace/settings/settings-sections.test.ts`

Expected: PASS。

- [ ] **Step 7: 提交设置弹窗变更**

```bash
git add frontend/src/components/workspace/settings/settings-sections.ts frontend/src/components/workspace/settings/settings-dialog.tsx frontend/src/components/workspace/workspace-nav-menu.tsx frontend/tests/unit/components/workspace/settings/settings-sections.test.ts
git commit -m "refactor(frontend): remove capabilities from settings dialog"
```

---

### Task 3: 新增三个独立工作区页面

**Files:**
- Create: `frontend/src/components/workspace/workspace-capability-page.tsx`
- Create: `frontend/src/app/workspace/memory/page.tsx`
- Create: `frontend/src/app/workspace/tools/page.tsx`
- Create: `frontend/src/app/workspace/skills/page.tsx`
- Test: `frontend/tests/unit/app/workspace/capability-pages.test.tsx`

**Interfaces:**
- Produces: `WorkspaceCapabilityPage({ children }: { children: React.ReactNode })`。
- Consumes: `WorkspaceContainer`、`WorkspaceHeader`、`WorkspaceBody` 与三个现有管理组件。

- [ ] **Step 1: 写页面映射失败测试**

```tsx
import { describe, expect, it } from "@rstest/core";
import type { ReactElement } from "react";

import MemoryPage from "@/app/workspace/memory/page";
import SkillsPage from "@/app/workspace/skills/page";
import ToolsPage from "@/app/workspace/tools/page";
import { MemorySettingsPage } from "@/components/workspace/settings/memory-settings-page";
import { SkillSettingsPage } from "@/components/workspace/settings/skill-settings-page";
import { ToolSettingsPage } from "@/components/workspace/settings/tool-settings-page";
import { WorkspaceCapabilityPage } from "@/components/workspace/workspace-capability-page";

describe("workspace capability pages", () => {
  it.each([
    [MemoryPage, MemorySettingsPage],
    [ToolsPage, ToolSettingsPage],
    [SkillsPage, SkillSettingsPage],
  ])("wraps the manager in the workspace page shell", (Page, Manager) => {
    const element = Page() as ReactElement<{ children: ReactElement }>;
    expect(element.type).toBe(WorkspaceCapabilityPage);
    expect(element.props.children.type).toBe(Manager);
  });
});
```

- [ ] **Step 2: 运行测试并确认因新路由不存在而失败**

Run: `cd frontend && pnpm test tests/unit/app/workspace/capability-pages.test.tsx`

Expected: FAIL，提示无法解析至少一个新页面模块。

- [ ] **Step 3: 实现统一页面容器**

```tsx
import {
  WorkspaceBody,
  WorkspaceContainer,
  WorkspaceHeader,
} from "@/components/workspace/workspace-container";

export function WorkspaceCapabilityPage({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <WorkspaceContainer>
      <WorkspaceHeader />
      <WorkspaceBody className="overflow-y-auto">
        <div className="w-full max-w-5xl p-6 md:p-8">{children}</div>
      </WorkspaceBody>
    </WorkspaceContainer>
  );
}
```

- [ ] **Step 4: 实现三个路由页面**

每个路由使用相同模式；记忆页完整实现如下，工具页和技能页分别替换管理组件：

```tsx
import { MemorySettingsPage } from "@/components/workspace/settings/memory-settings-page";
import { WorkspaceCapabilityPage } from "@/components/workspace/workspace-capability-page";

export default function MemoryPage() {
  return (
    <WorkspaceCapabilityPage>
      <MemorySettingsPage />
    </WorkspaceCapabilityPage>
  );
}
```

技能页渲染 `<SkillSettingsPage />`，不传 `onClose`。

- [ ] **Step 5: 运行页面映射测试并确认通过**

Run: `cd frontend && pnpm test tests/unit/app/workspace/capability-pages.test.tsx`

Expected: PASS。

- [ ] **Step 6: 提交独立页面变更**

```bash
git add frontend/src/components/workspace/workspace-capability-page.tsx frontend/src/app/workspace/memory/page.tsx frontend/src/app/workspace/tools/page.tsx frontend/src/app/workspace/skills/page.tsx frontend/tests/unit/app/workspace/capability-pages.test.tsx
git commit -m "feat(frontend): add workspace capability pages"
```

---

### Task 4: 文档同步与完整验证

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1-3 的最终导航与页面行为。
- Produces: 与新入口一致的用户说明。

- [ ] **Step 1: 更新记忆示例入口说明**

将 README 中用于评审记忆 fixture 的 `Settings > Memory` 改为 `Workspace sidebar > Memory`，不修改其他说明。

- [ ] **Step 2: 运行全部相关单元测试**

Run:

```bash
cd frontend && pnpm test \
  tests/unit/components/workspace/workspace-capability-links.test.ts \
  tests/unit/components/workspace/settings/settings-sections.test.ts \
  tests/unit/app/workspace/capability-pages.test.tsx
```

Expected: 三个测试文件全部 PASS。

- [ ] **Step 3: 运行前端静态检查**

Run: `cd frontend && pnpm check`

Expected: ESLint 和 `tsc --noEmit` 均退出码 0。

- [ ] **Step 4: 运行格式检查**

Run: `cd frontend && pnpm format`

Expected: Prettier 检查通过；若失败，仅对本次变更文件运行 `pnpm prettier --write <files>` 后重新检查。

- [ ] **Step 5: 本地验收导航和页面**

在 `http://localhost:2026` 验证：

1. 左侧“定时任务”后依次显示“记忆”“工具”“技能”。
2. 三个入口分别打开对应独立页面，当前项高亮。
3. 收起侧栏后仍显示三个图标。
4. “设置与更多 → 设置”中不再出现三项能力菜单。
5. 记忆操作、工具权限提示和技能创建入口仍正常展示。

- [ ] **Step 6: 提交文档并确认工作树状态**

```bash
git add README.md
git commit -m "docs: update workspace memory navigation"
git status --short
```

Expected: 只保留用户已有且与本功能无关的改动；本功能文件均已提交。
