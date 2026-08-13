import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AssetVersionDiff } from "@/components/assets/asset-version-diff";
import { projectAgentPreviousVersion } from "@/components/projects/assets/project-asset-detail-sheet";
import { I18nProvider } from "@/core/i18n/context";
import type { AssetVersion } from "@/core/shared-assets";

const AGENT_ID = "11111111-1111-4111-8111-111111111111";
const VERSION_1_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_2_ID = "33333333-3333-4333-8333-333333333333";

function agentVersion(
  id: string,
  versionNumber: number,
  supersedesVersionId: string | null,
  suffix: string,
): Extract<AssetVersion, { agent_id: string }> {
  return {
    id,
    agent_id: AGENT_ID,
    version_number: versionNumber,
    workflow_status: versionNumber === 1 ? "published" : "draft",
    description: `description-${suffix}`,
    agents_instructions: `AGENTS-${suffix}`,
    soul: `SOUL-${suffix}`,
    identity: `IDENTITY-${suffix}`,
    user_context: `USER-${suffix}`,
    payload_schema_version: 3,
    model_ref: "model-1",
    model_settings: { temperature: suffix === "before" ? 0.2 : 0.8 },
    tool_groups: ["task"],
    skill_version_ids: [],
    mcp_version_ids: [],
    supersedes_version_id: supersedesVersionId,
    payload_checksum: `checksum-${suffix}`,
    created_by_user_id: "44444444-4444-4444-8444-444444444444",
    created_at: "2026-08-13T00:00:00Z",
  };
}

describe("Agent version diff", () => {
  test("resolves the immediately preceding Agent revision without relying on list order", () => {
    const first = agentVersion(VERSION_1_ID, 1, null, "before");
    const second = agentVersion(VERSION_2_ID, 2, VERSION_1_ID, "after");

    expect(projectAgentPreviousVersion([second, first], second)).toBe(first);
    expect(projectAgentPreviousVersion([first, second], second)).toBe(first);
    expect(projectAgentPreviousVersion([second], second)).toBeNull();
    expect(projectAgentPreviousVersion([first, second], first)).toBeNull();
    expect(
      projectAgentPreviousVersion(
        [
          {
            ...first,
            agent_id: "55555555-5555-4555-8555-555555555555",
          },
          second,
        ],
        second,
      ),
    ).toBeNull();

    const third = agentVersion(
      "66666666-6666-4666-8666-666666666666",
      3,
      VERSION_1_ID,
      "third",
    );
    expect(projectAgentPreviousVersion([third, first, second], third)).toBe(
      second,
    );
  });

  test("includes all four governed Agent documents when explicitly requested", () => {
    const previous = agentVersion(VERSION_1_ID, 1, null, "before");
    const current = {
      ...agentVersion(VERSION_2_ID, 2, VERSION_1_ID, "after"),
      payload_schema_version: 4,
    };

    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AssetVersionDiff
          previous={previous}
          current={current}
          includeAgentDocuments
        />
      </I18nProvider>,
    );

    for (const documentName of [
      "AGENTS.md",
      "SOUL.md",
      "IDENTITY.md",
      "USER.md",
    ]) {
      expect(html).toContain(documentName);
    }
    expect(html).toContain("AGENTS-before");
    expect(html).toContain("AGENTS-after");
    expect(html).toContain("USER-before");
    expect(html).toContain("USER-after");
    expect(html).toContain("0.2");
    expect(html).toContain("0.8");
    expect(html).toContain("载荷结构版本");
  });

  test("keeps document bodies out of generic admin history by default", () => {
    const previous = agentVersion(VERSION_1_ID, 1, null, "before");
    const current = agentVersion(VERSION_2_ID, 2, VERSION_1_ID, "after");
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AssetVersionDiff previous={previous} current={current} />
      </I18nProvider>,
    );

    expect(html).not.toContain("AGENTS.md");
    expect(html).not.toContain("AGENTS-before");
  });
});
