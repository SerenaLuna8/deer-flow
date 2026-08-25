import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentBuilderBlueprintReview } from "@/components/projects/agents/agent-builder-blueprint-review";
import { AgentBuilderStartView } from "@/components/projects/agents/agent-builder-start";
import { ProjectAgentCatalogView } from "@/components/projects/assets/project-agents-page";
import {
  AgentSelectorDialog,
  type ExecutableProjectAgent,
} from "@/components/projects/private-work/agent-selector-dialog";
import { ProjectAgentStartContinuationView } from "@/components/projects/private-work/project-agent-start-continuation";
import { ThreadAgentIndicator } from "@/components/workspace/thread-agent-indicator";
import { I18nProvider } from "@/core/i18n/context";
import type { Capability } from "@/core/projects/types";

const PROJECT_CAPABILITIES = [
  "private_work.create",
  "shared_assets.execute",
] satisfies Capability[];

const AGENT: ExecutableProjectAgent = {
  id: "00000000-0000-4000-8000-000000000001",
  scope: "project",
  project_id: "00000000-0000-4000-8000-000000000002",
  slug: "reviewer",
  display_name: "Reviewer",
  description: "Reviews changes before release.",
  status: "active",
  definition_id: "00000000-0000-4000-8000-000000000003",
  revision: 1,
  capabilities: PROJECT_CAPABILITIES,
  binding: null,
  created_by_user_id: "user-1",
  created_at: "2026-08-13T00:00:00Z",
  updated_at: "2026-08-13T00:00:00Z",
};

describe("Agent lifecycle translations", () => {
  test("keeps blocked Agent remediation inside the editor workspace", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <AgentSelectorDialog
          open
          agents={[]}
          blockedSystemAgents={[AGENT]}
          canAuthorProjectAgent={false}
          agentsPath="/projects/alpha/agents?intent=start_chat"
          isCreating={false}
          onOpenChange={() => undefined}
          onSelect={() => undefined}
        />
      </I18nProvider>,
    );

    expect(html).toContain("Contact project editor");
    expect(html).not.toContain(
      'href="/projects/alpha/agents?intent=start_chat"',
    );
  });

  test("renders key en-US lifecycle surfaces without Chinese fallback copy", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <AgentBuilderStartView
          name="reviewer"
          normalizedName="reviewer"
          errorMessage={null}
          pending={false}
          onNameChange={() => undefined}
          onSubmit={() => undefined}
        />
        <AgentBuilderBlueprintReview
          blueprint={{
            description: "Reviews changes before release.",
            model_ref: "default",
            tool_groups: ["read"],
            skill_refs: [],
            mcp_version_ids: [],
            agents_instructions: "# Instructions",
            soul: "# Voice",
            identity: "# Identity",
            user_context: "# User",
          }}
          agentName="reviewer"
          agentSlug="reviewer"
          agentSlugError={null}
          models={[]}
          canAuthor={false}
          editing={false}
          pending={false}
          creating={false}
          dirty={false}
          canCreate={false}
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
        />
        <ProjectAgentCatalogView
          systemItems={[]}
          projectItems={[AGENT]}
          projectCapabilities={PROJECT_CAPABILITIES}
          viewMode="cards"
          selectedAssetId={null}
          creatingChatForAgentId={null}
          onSelect={() => undefined}
          onStartChat={() => undefined}
        />
        <AgentSelectorDialog
          open
          agents={[AGENT]}
          isCreating={false}
          onOpenChange={() => undefined}
          onSelect={() => undefined}
        />
        <ProjectAgentStartContinuationView status="waiting-for-service" />
        <ThreadAgentIndicator
          identity={{ displayName: "Reviewer", available: true }}
          onStartNewChat={() => undefined}
        />
      </I18nProvider>,
    );

    expect(html).toContain("Name your new Agent");
    expect(html).toContain("Agent blueprint");
    expect(html).toContain("Agent name");
    expect(html).not.toContain("both its name and slug");
    expect(html).toContain("Project Agents");
    expect(html).toContain("Choose an Agent");
    expect(html).toContain("Waiting for the service");
    expect(html).toContain("Choose another Agent to start a new chat");
    expect(html).not.toMatch(/[\p{Script=Han}]/u);
  });
});
