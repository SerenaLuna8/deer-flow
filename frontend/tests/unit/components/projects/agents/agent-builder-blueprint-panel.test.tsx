import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AgentBuilderBlueprintReview,
  agentBuilderBlueprintTabForKey,
} from "@/components/projects/agents/agent-builder-blueprint-review";
import { AgentBuilderBlueprintSummaryCard } from "@/components/projects/agents/agent-builder-blueprint-summary";
import { AgentBuilderBlueprintTrigger } from "@/components/projects/agents/agent-builder-blueprint-trigger";
import type { AgentBuilderBlueprint } from "@/core/agent-builder";
import { I18nProvider } from "@/core/i18n/context";
import type { Model } from "@/core/models/types";

function renderUi(node: React.ReactNode) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{node}</I18nProvider>,
  );
}

const blueprint: AgentBuilderBlueprint = {
  description: "审查代码并给出可执行建议",
  model_ref: "default",
  tool_groups: ["read"],
  skill_refs: [],
  mcp_version_ids: [],
  agents_instructions: "# AGENTS.md\n\n审查代码。",
  soul: "# SOUL.md\n\n保持严谨。",
  identity: "# IDENTITY.md\n\n代码审查 Agent。",
  user_context: "# USER.md\n\n偏好中文。",
};

const models: Model[] = [
  {
    name: "default",
    model: "default",
    display_name: "GPT-5.6 Luna",
    supports_thinking: true,
    supports_reasoning_effort: true,
    supports_vision: true,
    supports_vision_bridge: false,
    is_default: true,
  },
];

describe("AgentBuilderBlueprintTrigger", () => {
  test("appears only after a blueprint exists and reports blocking conflicts", () => {
    expect(
      renderUi(
        <AgentBuilderBlueprintTrigger
          available={false}
          conflictCount={0}
          onOpen={() => undefined}
        />,
      ),
    ).toBe("");

    const html = renderUi(
      <AgentBuilderBlueprintTrigger
        available
        conflictCount={2}
        onOpen={() => undefined}
      />,
    );

    expect(html).toContain('data-testid="agent-builder-blueprint-trigger"');
    expect(html).toContain('aria-label="查看 Agent 设计稿"');
    expect(html).toContain("设计稿");
    expect(html).toContain('aria-label="2 项待解决冲突"');
  });
});

describe("AgentBuilderBlueprintSummaryCard", () => {
  test("keeps only a compact blueprint handoff in the conversation", () => {
    const html = renderUi(
      <AgentBuilderBlueprintSummaryCard
        conflictCount={1}
        onOpen={() => undefined}
      />,
    );

    expect(html).toContain('data-testid="agent-builder-blueprint-summary"');
    expect(html).toContain("Agent 设计稿已就绪");
    expect(html).toContain("4 个文档 · 1 项待解决冲突");
    expect(html).toContain("查看设计稿");
  });
});

describe("AgentBuilderBlueprintReview", () => {
  test("presents the blueprint as a dismissible overview and documents panel", () => {
    const html = renderUi(
      <AgentBuilderBlueprintReview
        blueprint={blueprint}
        agentName="Code Review"
        agentSlug="code-review"
        agentSlugError={null}
        models={models}
        assumptions={["仅审查当前项目代码"]}
        conflicts={[]}
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
        onClose={() => undefined}
      />,
    );

    expect(html).toContain('data-testid="agent-builder-blueprint-panel"');
    expect(html).toContain('aria-label="Agent 设计稿"');
    expect(html).toContain('aria-label="关闭 Agent 设计稿"');
    expect(html).toContain('role="tablist"');
    expect(html).toContain("概览");
    expect(html).toContain("文档");
    expect(html).toContain("4 个文档");
    expect(html).toContain("AGENTS.md");
    expect(html).toContain("USER.md");
    expect(html).toMatch(
      /data-agent-builder-blueprint-footer="true"[^>]*class="[^"]*pb-4/u,
    );
  });

  test("supports the horizontal keyboard contract for its two surfaces", () => {
    expect(agentBuilderBlueprintTabForKey("overview", "ArrowRight")).toBe(
      "documents",
    );
    expect(agentBuilderBlueprintTabForKey("documents", "ArrowLeft")).toBe(
      "overview",
    );
    expect(agentBuilderBlueprintTabForKey("documents", "Home")).toBe(
      "overview",
    );
    expect(agentBuilderBlueprintTabForKey("overview", "End")).toBe("documents");
    expect(agentBuilderBlueprintTabForKey("overview", "Enter")).toBeNull();
  });
});
