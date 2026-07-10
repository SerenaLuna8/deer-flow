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
