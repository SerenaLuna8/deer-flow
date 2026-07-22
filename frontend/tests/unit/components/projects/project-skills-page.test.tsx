import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SkillAssetDetail } from "@/components/projects/assets/skill-asset-detail";
import type { AssetVersion } from "@/core/shared-assets";

const version: Extract<AssetVersion, { skill_id: string }> = {
  id: "11111111-1111-4111-8111-111111111111",
  skill_id: "22222222-2222-4222-8222-222222222222",
  version_number: 2,
  workflow_status: "published",
  description: "Review academic papers",
  frontmatter: { name: "paper-review", category: "research" },
  compatibility: ">=2.1",
  secret_requirements: [{ name: "SEARCH_TOKEN", optional: true }],
  scan_decision: "warn",
  scan_rule_ids: ["external-network"],
  scan_summary: { warnings: 1 },
  file_views: [
    {
      path: "SKILL.md",
      media_type: "text/markdown",
      size_bytes: 2048,
      sha256: "b".repeat(64),
    },
  ],
  supersedes_version_id: null,
  payload_checksum: "a".repeat(64),
  created_by_user_id: "editor-1",
  created_at: "2026-07-21T00:00:00Z",
};

describe("Skill asset detail", () => {
  test("shows scan, compatibility and file metadata without pretending file content is readable", () => {
    const html = renderToStaticMarkup(<SkillAssetDetail version={version} />);

    for (const text of [
      "Review academic papers",
      "兼容性",
      "扫描结果",
      "external-network",
      "SKILL.md",
      "text/markdown",
      "2 KB",
      "SEARCH_TOKEN",
    ]) {
      expect(html).toContain(text);
    }
    for (const unsupported of ["文件正文", "安装技能", "被 3 个 Agent 使用"]) {
      expect(html).not.toContain(unsupported);
    }
  });
});
