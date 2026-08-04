import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectAssetDetailHeader } from "@/components/projects/assets/project-asset-detail-sheet";
import {
  adminProjectAssetDetailLifecycleActions,
  projectAssetCanCreateVersion,
  projectAssetCanDelete,
  projectAssetDetailLifecycleActions,
} from "@/components/projects/assets/project-asset-view-model";
import { Sheet } from "@/components/ui/sheet";
import type { ProjectAssetItem } from "@/core/shared-assets";

const AGENT: ProjectAssetItem = {
  id: "22222222-2222-4222-8222-222222222222",
  scope: "project",
  project_id: "33333333-3333-4333-8333-333333333333",
  slug: "project-agent",
  display_name: "Project Agent",
  description: "项目 Agent",
  status: "active",
  current_published_version_id: "44444444-4444-4444-8444-444444444444",
  version: 3,
  created_by_user_id: "editor",
  created_at: "2026-07-20T00:00:00Z",
  updated_at: "2026-07-21T00:00:00Z",
  capabilities: [
    "shared_assets.read",
    "shared_assets.edit",
    "shared_assets.manage_bindings",
  ],
  binding: null,
};

describe("Agent detail simplification", () => {
  test("removes the dead generic project Agent create and version surface", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/assets/project-assets-page.tsx",
      ),
      "utf8",
    );

    expect(source).not.toContain("function MutableProjectAssets");
    expect(source).not.toContain("function ProjectAssetsPage");
    expect(source).not.toContain("CreateAssetDialog");
    expect(source).not.toContain("CreateVersionDialog");
  });

  test("shows only the Agent name and update time in the detail header", () => {
    const html = renderToStaticMarkup(
      <Sheet open>
        <ProjectAssetDetailHeader
          kind="agents"
          item={AGENT}
          statusPending={false}
          onToggleProjectSkillStatus={() => undefined}
        />
      </Sheet>,
    );

    expect(html).toContain("Project Agent");
    expect(html).toContain(`dateTime="${AGENT.updated_at}"`);
    expect(html).toContain(new Date(AGENT.updated_at).toLocaleString("zh-CN"));
    expect(html).not.toContain("project-agent");
    expect(html).not.toContain("项目自建");
    expect(html).not.toContain("系统提供");
    expect(html).not.toContain("启用");
    expect(html).not.toContain("已暂停");
  });

  test("removes Agent version creation and archive while retaining reversible activation", () => {
    expect(projectAssetCanCreateVersion("agents", true)).toBe(false);
    expect(projectAssetCanCreateVersion("mcp-servers", true)).toBe(true);
    expect(projectAssetCanCreateVersion("mcp-servers", false)).toBe(false);

    expect(
      projectAssetDetailLifecycleActions("agents", AGENT, AGENT.capabilities),
    ).toEqual(["suspend"]);
    expect(
      projectAssetDetailLifecycleActions(
        "agents",
        { ...AGENT, status: "suspended" },
        AGENT.capabilities,
      ),
    ).toEqual(["activate"]);
    expect(
      projectAssetDetailLifecycleActions(
        "agents",
        {
          ...AGENT,
          capabilities: AGENT.capabilities.filter(
            (capability) => capability !== "shared_assets.manage_bindings",
          ),
        },
        AGENT.capabilities,
      ),
    ).toEqual([]);
    expect(
      adminProjectAssetDetailLifecycleActions("mcp-servers", AGENT),
    ).toEqual(["archive", "suspend"]);
    expect(
      projectAssetDetailLifecycleActions(
        "agents",
        { ...AGENT, current_published_version_id: null },
        AGENT.capabilities,
      ),
    ).toEqual(["suspend"]);
    expect(
      projectAssetDetailLifecycleActions(
        "agents",
        {
          ...AGENT,
          status: "suspended",
          current_published_version_id: null,
        },
        AGENT.capabilities,
      ),
    ).toEqual([]);
    expect(
      projectAssetDetailLifecycleActions(
        "mcp-servers",
        AGENT,
        AGENT.capabilities,
      ),
    ).toEqual(["suspend"]);
    expect(
      projectAssetDetailLifecycleActions(
        "mcp-servers",
        { ...AGENT, status: "suspended" },
        AGENT.capabilities,
      ),
    ).toEqual(["activate"]);
    expect(
      projectAssetDetailLifecycleActions(
        "mcp-servers",
        { ...AGENT, status: "archived" },
        AGENT.capabilities,
      ),
    ).toEqual([]);
    expect(
      projectAssetDetailLifecycleActions(
        "mcp-servers",
        { ...AGENT, current_published_version_id: null },
        AGENT.capabilities,
      ),
    ).toEqual([]);
  });

  test("allows permanent deletion only for editable project Agents", () => {
    expect(projectAssetCanDelete("agents", AGENT)).toBe(true);
    expect(projectAssetCanDelete("skills", AGENT)).toBe(true);
    expect(projectAssetCanDelete("mcp-servers", AGENT)).toBe(true);
    expect(projectAssetCanDelete("agents", { ...AGENT, scope: "system" })).toBe(
      false,
    );
    expect(
      projectAssetCanDelete("mcp-servers", { ...AGENT, scope: "system" }),
    ).toBe(false);
    expect(
      projectAssetCanDelete("agents", {
        ...AGENT,
        capabilities: AGENT.capabilities.filter(
          (capability) => capability !== "shared_assets.edit",
        ),
      }),
    ).toBe(false);
  });
});
