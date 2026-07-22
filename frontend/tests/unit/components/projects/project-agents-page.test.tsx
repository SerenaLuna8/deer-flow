import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentAssetDetail } from "@/components/projects/assets/agent-asset-detail";
import type { AssetVersion } from "@/core/shared-assets";

const version: Extract<AssetVersion, { agent_id: string }> = {
  id: "11111111-1111-4111-8111-111111111111",
  agent_id: "22222222-2222-4222-8222-222222222222",
  version_number: 4,
  workflow_status: "published",
  description: "Research and compare sources",
  soul: "Be careful and cite evidence.",
  model_ref: "deepseek-v4-pro",
  tool_groups: ["web", "files"],
  skill_version_ids: ["33333333-3333-4333-8333-333333333333"],
  mcp_version_ids: ["44444444-4444-4444-8444-444444444444"],
  supersedes_version_id: null,
  payload_checksum: "a".repeat(64),
  created_by_user_id: "editor-1",
  created_at: "2026-07-21T00:00:00Z",
};

describe("Agent asset detail", () => {
  test("shows the actual Agent definition without inventing runtime analytics", () => {
    const html = renderToStaticMarkup(<AgentAssetDetail version={version} />);

    for (const text of [
      "Research and compare sources",
      "Be careful and cite evidence.",
      "deepseek-v4-pro",
      "web",
      "files",
      "Skill 依赖",
      "MCP 依赖",
    ]) {
      expect(html).toContain(text);
    }
    for (const unsupported of ["成功率", "运行次数", "平均耗时"]) {
      expect(html).not.toContain(unsupported);
    }
  });
});
