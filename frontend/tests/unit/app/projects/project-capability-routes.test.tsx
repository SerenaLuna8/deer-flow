import { beforeEach, describe, expect, rs, test } from "@rstest/core";

rs.mock("@/core/projects/server-capability", () => ({
  requireServerProjectCapability: rs.fn(async () => undefined),
}));
rs.mock("@/components/projects/assets/project-agents-page", () => ({
  ProjectAgentsPage: () => null,
}));
rs.mock("@/components/projects/agents/agent-builder-start", () => ({
  AgentBuilderStart: () => null,
}));
rs.mock("@/components/projects/agents/agent-builder-workspace", () => ({
  AgentBuilderWorkspace: () => null,
}));
rs.mock("@/components/projects/assets/project-skills-page", () => ({
  ProjectSkillsPage: () => null,
}));
rs.mock("@/components/projects/skills/skill-builder-start", () => ({
  SkillBuilderStart: () => null,
}));
rs.mock("@/components/projects/skills/skill-builder-workspace", () => ({
  SkillBuilderWorkspace: () => null,
}));
rs.mock("@/components/projects/assets/project-mcp-page", () => ({
  ProjectMcpPage: () => null,
}));

import ProjectAgentBuilderSessionPage from "@/app/projects/[project_slug]/agents/new/[session_id]/page";
import NewProjectAgentPage from "@/app/projects/[project_slug]/agents/new/page";
import ProjectAgentsPage from "@/app/projects/[project_slug]/agents/page";
import ProjectConnectionsLayout from "@/app/projects/[project_slug]/connections/layout";
import ProjectMcpPage from "@/app/projects/[project_slug]/mcp/page";
import ProjectSkillBuilderSessionPage from "@/app/projects/[project_slug]/skills/new/[session_id]/page";
import ProjectSkillBuilderStartPage from "@/app/projects/[project_slug]/skills/new/page";
import ProjectSkillsPage from "@/app/projects/[project_slug]/skills/page";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

const requireCapability = rs.mocked(requireServerProjectCapability);
const params = Promise.resolve({ project_slug: "alpha" });

describe("project capability workspace routes", () => {
  beforeEach(() => {
    requireCapability.mockClear();
  });

  test.each([
    [
      "Agent catalog",
      () => ProjectAgentsPage({ params, searchParams: Promise.resolve({}) }),
      "shared_assets.read",
    ],
    ["new Agent", () => NewProjectAgentPage({ params }), "shared_assets.edit"],
    [
      "Agent Builder session",
      () =>
        ProjectAgentBuilderSessionPage({
          params: Promise.resolve({
            project_slug: "alpha",
            session_id: "agent-session",
          }),
        }),
      "shared_assets.read",
    ],
    [
      "Skill catalog",
      () => ProjectSkillsPage({ params, searchParams: Promise.resolve({}) }),
      "shared_assets.read",
    ],
    [
      "new Skill",
      () => ProjectSkillBuilderStartPage({ params }),
      "shared_assets.edit",
    ],
    [
      "Skill Builder session",
      () =>
        ProjectSkillBuilderSessionPage({
          params: Promise.resolve({
            project_slug: "alpha",
            session_id: "skill-session",
          }),
        }),
      "shared_assets.read",
    ],
    [
      "MCP catalog",
      () => ProjectMcpPage({ params }),
      ["shared_assets.edit", "shared_assets.manage_bindings"],
    ],
  ])(
    "requires asset workspace authority for %s",
    async (_label, renderRoute, expectedCapability) => {
      await renderRoute();

      expect(requireCapability).toHaveBeenCalledWith(
        "alpha",
        expectedCapability,
      );
    },
  );

  test("requires project channel management authority for Connections", async () => {
    await ProjectConnectionsLayout({ children: null, params });

    expect(requireCapability).toHaveBeenCalledWith(
      "alpha",
      "project.channels.manage",
    );
  });

  test("validates the exact Skill version and Credential-focus deep link", async () => {
    const skillId = "11111111-1111-4111-8111-111111111111";
    const versionId = "22222222-2222-4222-8222-222222222222";
    const rendered = await ProjectSkillsPage({
      params,
      searchParams: Promise.resolve({
        skill_id: skillId,
        skill_version_id: versionId,
        configure_credentials: "1",
      }),
    });

    expect(rendered.props).toMatchObject({
      selectedAssetId: skillId,
      selectedVersionId: versionId,
      focusSelectedSkillCredentials: true,
    });

    const rejected = await ProjectSkillsPage({
      params,
      searchParams: Promise.resolve({
        skill_id: skillId,
        skill_version_id: [versionId, versionId],
        configure_credentials: "true",
      }),
    });
    expect(rejected.props).toMatchObject({
      selectedAssetId: skillId,
      selectedVersionId: null,
      focusSelectedSkillCredentials: false,
    });
  });
});
