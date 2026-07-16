import { describe, expect, it } from "@rstest/core";
import type { ReactElement } from "react";

import MemoryPage from "@/app/workspace/memory/page";
import SkillsPage from "@/app/workspace/skills/page";
import ToolsPage from "@/app/workspace/tools/page";
import { LegacySystemAssetsCompatibility } from "@/components/workspace/legacy-system-assets-compatibility";
import { MemorySettingsPage } from "@/components/workspace/settings/memory-settings-page";
import { WorkspaceCapabilityPage } from "@/components/workspace/workspace-capability-page";

describe("workspace capability pages", () => {
  it("wraps the memory manager in the workspace page shell", () => {
    const element = MemoryPage() as ReactElement<{ children: ReactElement }>;
    expect(element.type).toBe(WorkspaceCapabilityPage);
    expect(element.props.children.type).toBe(MemorySettingsPage);
  });

  it.each([
    [ToolsPage, "MCP"],
    [SkillsPage, "Skill"],
  ] as const)(
    "renders legacy system assets as read-only compatibility views",
    (Page, kind) => {
      const element = Page() as ReactElement<{
        kind: "Agent" | "Skill" | "MCP";
      }>;
      expect(element.type).toBe(LegacySystemAssetsCompatibility);
      expect(element.props.kind).toBe(kind);
    },
  );
});
