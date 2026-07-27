import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  SkillAssetDetail,
  skillCredentialBindingsVisible,
} from "@/components/projects/assets/skill-asset-detail";
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
  test("shows Credential bindings only for the selected current published version", () => {
    expect(skillCredentialBindingsVisible(version.id, version.id)).toBe(true);
    expect(
      skillCredentialBindingsVisible(
        "33333333-3333-4333-8333-333333333333",
        version.id,
      ),
    ).toBe(false);
    expect(skillCredentialBindingsVisible(version.id, null)).toBe(false);
  });

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

  test("bounds the duplicate metadata snapshot for a very large Skill version", () => {
    const largeVersion = {
      ...version,
      file_views: Array.from({ length: 12_139 }, (_, index) => ({
        path: `templates/assets/file-${String(index).padStart(5, "0")}.txt`,
        media_type: "text/plain",
        size_bytes: index + 1,
        sha256: `${index}`.padStart(64, "0"),
      })),
    };

    const html = renderToStaticMarkup(
      <SkillAssetDetail version={largeVersion} />,
    );

    expect(html).toContain("共 12139 个文件");
    expect(html).toContain("file-00000.txt");
    expect(html).toContain("file-00019.txt");
    expect(html).not.toContain("file-00020.txt");
    expect(html).not.toContain("file-12138.txt");
    expect(html).toContain("其余 12119 个文件");
    expect(html.match(/SHA-256/g)).toHaveLength(20);
  });
});
