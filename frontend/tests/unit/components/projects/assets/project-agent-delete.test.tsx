import { describe, expect, test } from "@rstest/core";

import { projectAgentDeleteBlockedReason } from "@/components/projects/assets/project-asset-detail-sheet";
import { projectAgentDeleteErrorMessage } from "@/components/projects/assets/project-asset-view-model";
import { projectAssetDeleteDescription } from "@/components/projects/assets/project-skill-delete-dialog";
import { SharedAssetApiError } from "@/core/shared-assets";

const AGENT_ID = "00000000-0000-4000-8000-000000000020";

describe("project Agent deletion", () => {
  test("blocks deletion while the Agent is the current project default", () => {
    expect(
      projectAgentDeleteBlockedReason(AGENT_ID, AGENT_ID, false, false),
    ).toBe("当前默认 Agent 无法删除，请先将 Main 或其他 Agent 设为默认。");

    expect(projectAgentDeleteBlockedReason(AGENT_ID, null, false, false)).toBe(
      null,
    );
  });

  test("fails closed while the default selection cannot be confirmed", () => {
    expect(
      projectAgentDeleteBlockedReason(AGENT_ID, undefined, true, false),
    ).toBe("正在确认项目默认 Agent，请稍候。");
    expect(
      projectAgentDeleteBlockedReason(AGENT_ID, undefined, false, true),
    ).toBe("无法确认项目默认 Agent，请刷新后重试。");
  });

  test("maps a delete conflict to actionable Agent-specific guidance", () => {
    expect(
      projectAgentDeleteErrorMessage(
        new SharedAssetApiError(409, "ASSET_CONFLICT", "Asset state conflict"),
      ),
    ).toBe(
      "无法删除此 Agent：它可能是项目当前默认 Agent、已被对话、自动化或运行记录引用，或状态已经变化。请刷新页面并处理相关引用后重试。",
    );
  });

  test("warns about the current-default restriction before confirmation", () => {
    const description = projectAssetDeleteDescription("Agent", "code-reviewer");

    expect(description).toContain("若该 Agent 是项目当前默认 Agent");
    expect(description).toContain("请先将 Main 或其他 Agent 设为默认");
  });
});
