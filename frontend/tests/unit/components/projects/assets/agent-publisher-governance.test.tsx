import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentBuilderBlueprintReview } from "@/components/projects/agents/agent-builder-blueprint-review";
import { AgentInstructionWorkspace } from "@/components/projects/assets/agent-instructions-workbench";
import {
  projectAgentVersionCanActivate,
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

describe("Agent author and activation governance", () => {
  test("lets an Editor or Admin activate a project candidate", () => {
    const candidate = { relation: "candidate" as const };
    const editorItem = {
      scope: "project" as const,
      capabilities: [EDIT],
      current_version_id: null,
    };
    const adminItem = {
      scope: "project" as const,
      capabilities: [EDIT, MANAGE_BINDINGS],
      current_version_id: null,
    };

    expect(projectAgentVersionCanActivate(editorItem, [EDIT], candidate)).toBe(
      true,
    );
    expect(
      projectAgentVersionCanActivate(
        adminItem,
        [EDIT, MANAGE_BINDINGS],
        candidate,
      ),
    ).toBe(true);
    expect(
      projectAgentVersionCanActivate(adminItem, [EDIT, MANAGE_BINDINGS], {
        relation: "current",
      }),
    ).toBe(false);
    expect(
      projectAgentVersionCanActivate(
        { ...adminItem, scope: "system" },
        [EDIT, MANAGE_BINDINGS],
        candidate,
      ),
    ).toBe(false);
    expect(
      projectAgentVersionCanActivate(
        {
          ...adminItem,
        },
        [EDIT, MANAGE_BINDINGS],
        candidate,
      ),
    ).toBe(true);
  });

  test("lets an Editor delete an Agent asset regardless of Current Version", () => {
    expect(
      projectAssetCanDelete("agents", {
        scope: "project",
        capabilities: [EDIT],
        current_version_id: null,
      }),
    ).toBe(true);
    expect(
      projectAssetCanDelete("agents", {
        scope: "project",
        capabilities: [EDIT],
        current_version_id: "11111111-1111-4111-8111-111111111111",
      }),
    ).toBe(true);
    expect(
      projectAssetCanDelete("agents", {
        scope: "project",
        capabilities: [EDIT, MANAGE_BINDINGS],
        current_version_id: "11111111-1111-4111-8111-111111111111",
      }),
    ).toBe(true);
  });

  test("explains that Builder saves an inactive v1 Candidate Version", () => {
    const html = renderAgentUi(
      <AgentBuilderBlueprintReview
        blueprint={{
          description: "Review changes",
          model_ref: "default",
          tool_groups: ["file:read"],
          skill_refs: [],
          mcp_version_ids: [],
          agents_instructions: "# AGENTS",
          soul: "# SOUL",
          identity: "# IDENTITY",
          user_context: "# USER",
          model_settings: {},
        }}
        agentName="reviewer"
        agentSlug="reviewer"
        agentSlugError={null}
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
        onAgentNameChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onCreate={() => undefined}
      />,
    );

    expect(html).toContain("不可变的 v1 候选版本");
    expect(html).toContain("激活后才会用于运行");
    expect(html).not.toContain("创建后默认停用，需手动启用");
  });

  test("describes an instruction save as a Candidate Version without activation", () => {
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

    expect(html).toContain("保存后创建不可变的候选版本");
    expect(html).toContain("激活后才会用于后续运行");
    expect(html).not.toContain("保存后将用于后续运行");
  });

  test("describes System Agent instructions as an immutable read-only v1", () => {
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
        editing={false}
        canEdit={false}
        readOnly
        pending={false}
        dirty={false}
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

    expect(html).toContain("系统 Agent 的唯一 v1 指令文档，只读展示");
    expect(html).not.toContain("保存后创建不可变的候选版本");
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
