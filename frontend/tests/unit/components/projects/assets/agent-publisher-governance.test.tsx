import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentBuilderBlueprintReview } from "@/components/projects/agents/agent-builder-blueprint-review";
import { AgentInstructionWorkspace } from "@/components/projects/assets/agent-instructions-workbench";
import {
  projectAgentVersionCanPublish,
  projectAssetCanDelete,
} from "@/components/projects/assets/project-asset-view-model";
import { I18nProvider } from "@/core/i18n/context";

const MANAGE_BINDINGS = "shared_assets.manage_bindings" as const;
const EDIT = "shared_assets.edit" as const;

function renderAgentUi(node: React.ReactNode) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{node}</I18nProvider>,
  );
}

describe("Agent author and publisher governance", () => {
  test("only a project binding manager can publish a selected draft", () => {
    const draft = {
      workflow_status: "draft" as const,
      supersedes_version_id: null,
    };
    const editorItem = {
      scope: "project" as const,
      capabilities: [EDIT],
      current_published_version_id: null,
    };
    const publisherItem = {
      scope: "project" as const,
      capabilities: [MANAGE_BINDINGS],
      current_published_version_id: null,
    };

    expect(projectAgentVersionCanPublish(editorItem, [EDIT], draft)).toBe(
      false,
    );
    expect(
      projectAgentVersionCanPublish(publisherItem, [MANAGE_BINDINGS], draft),
    ).toBe(true);
    expect(
      projectAgentVersionCanPublish(publisherItem, [MANAGE_BINDINGS], {
        workflow_status: "published",
        supersedes_version_id: null,
      }),
    ).toBe(false);
    expect(
      projectAgentVersionCanPublish(
        { ...publisherItem, scope: "system" },
        [MANAGE_BINDINGS],
        draft,
      ),
    ).toBe(false);
    expect(
      projectAgentVersionCanPublish(
        {
          ...publisherItem,
          current_published_version_id: "22222222-2222-4222-8222-222222222222",
        },
        [MANAGE_BINDINGS],
        draft,
      ),
    ).toBe(false);
  });

  test("requires publisher authority before deleting a published Agent", () => {
    expect(
      projectAssetCanDelete("agents", {
        scope: "project",
        capabilities: [EDIT],
        current_published_version_id: null,
      }),
    ).toBe(true);
    expect(
      projectAssetCanDelete("agents", {
        scope: "project",
        capabilities: [EDIT],
        current_published_version_id: "11111111-1111-4111-8111-111111111111",
      }),
    ).toBe(false);
    expect(
      projectAssetCanDelete("agents", {
        scope: "project",
        capabilities: [EDIT, MANAGE_BINDINGS],
        current_published_version_id: "11111111-1111-4111-8111-111111111111",
      }),
    ).toBe(true);
  });

  test("explains that Builder creates a suspended draft", () => {
    const html = renderAgentUi(
      <AgentBuilderBlueprintReview
        blueprint={{
          description: "Review changes",
          model_ref: "default",
          tool_groups: ["file:read"],
          skill_version_ids: [],
          mcp_version_ids: [],
          agents_instructions: "# AGENTS",
          soul: "# SOUL",
          identity: "# IDENTITY",
          user_context: "# USER",
          model_settings: {},
        }}
        models={[]}
        canAuthor
        editing={false}
        pending={false}
        creating={false}
        dirty={false}
        canCreate
        selectedField="agents_instructions"
        displayMode="preview"
        errorMessage={null}
        onSelectedFieldChange={() => undefined}
        onDisplayModeChange={() => undefined}
        onBlueprintChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onCreate={() => undefined}
      />,
    );

    expect(html).toContain("创建停用的 Agent 草稿");
    expect(html).toContain("管理员发布草稿后才能启用");
    expect(html).not.toContain("创建后默认停用，需手动启用");
  });

  test("describes an instruction save as a draft, not an immediate publish", () => {
    const html = renderAgentUi(
      <AgentInstructionWorkspace
        draft={{
          agents_instructions: "# AGENTS",
          soul: "# SOUL",
          identity: "# IDENTITY",
          user_context: "# USER",
        }}
        selectedField="agents_instructions"
        displayMode="preview"
        editing
        canEdit
        pending={false}
        dirty
        errorMessage={null}
        saveDisabledReason={null}
        onSelect={() => undefined}
        onDisplayModeChange={() => undefined}
        onChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
      />,
    );

    expect(html).toContain("保存后创建草稿");
    expect(html).toContain("不会直接发布");
    expect(html).not.toContain("保存后将用于后续运行");
  });

  test("freezes instruction edits while a save is pending", () => {
    const html = renderAgentUi(
      <AgentInstructionWorkspace
        draft={{
          agents_instructions: "# AGENTS",
          soul: "# SOUL",
          identity: "# IDENTITY",
          user_context: "# USER",
        }}
        selectedField="agents_instructions"
        displayMode="source"
        editing
        canEdit
        pending
        dirty
        errorMessage={null}
        saveDisabledReason={null}
        onSelect={() => undefined}
        onDisplayModeChange={() => undefined}
        onChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
      />,
    );

    expect(html).toMatch(/<textarea[^>]*disabled=""/);
    expect(html).toContain("保存中…");
  });
});
