import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentDefinitionSummary } from "@/components/admin/assets/admin-asset-page";
import { I18nProvider } from "@/core/i18n/context";
import type { AgentDefinition } from "@/core/shared-assets";

const definition = {
  definition_id: "11111111-1111-4111-8111-111111111111",
  agent_id: "22222222-2222-4222-8222-222222222222",
  description: "Admin Definition presentation",
  agents_instructions: "# AGENTS",
  soul: "# SOUL",
  identity: "# IDENTITY",
  user_context: "# USER",
  payload_schema_version: 3,
  model_ref: "default",
  model_settings: {},
  tool_groups: ["file:read"],
  skill_refs: [],
  mcp_version_ids: [],
  payload_checksum: "a".repeat(64),
  updated_by_user_id: "33333333-3333-4333-8333-333333333333",
  updated_at: "2026-08-16T00:00:00Z",
} satisfies AgentDefinition;

describe("admin Agent Definition presentation", () => {
  test("renders the one immutable System Definition without version history", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AgentDefinitionSummary definition={definition} scope="system" />
      </I18nProvider>,
    );

    expect(html).toContain("系统 Agent 的唯一不可变 Definition");
    expect(html).toContain("Admin Definition presentation");
    expect(html).toContain("AGENTS.md");
    expect(html).toContain("# AGENTS");
    expect(html).not.toContain("Candidate Version");
    expect(html).not.toContain("候选版本");
  });

  test("describes a Project Definition as mutable and labels Skill asset references", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AgentDefinitionSummary
          definition={{
            ...definition,
            skill_refs: [
              {
                scope: "project",
                asset_id: "44444444-4444-4444-8444-444444444444",
              },
            ],
          }}
          scope="project"
        />
      </I18nProvider>,
    );

    expect(html).toContain("当前项目 Agent 的可变 Definition");
    expect(html).toContain("Skill 资产");
    expect(html).toContain("project:44444444-4444-4444-8444-444444444444");
    expect(html).not.toContain("系统 Agent 的唯一不可变 Definition");
    expect(html).not.toContain("Skill 版本");
  });
});
