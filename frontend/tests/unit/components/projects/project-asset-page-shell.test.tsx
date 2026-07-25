import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  defaultProjectAssetSource,
  filterProjectAssetItems,
  projectAssetSourceOptions,
  ProjectAssetListView,
  systemBindingToggleState,
} from "@/components/projects/assets/project-asset-page-shell";
import type { ProjectAssetList } from "@/core/shared-assets";

const PROJECT_ID = "33333333-3333-4333-8333-333333333333";
const SYSTEM_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ASSET_ID = "22222222-2222-4222-8222-222222222222";
const LATEST_VERSION_ID = "44444444-4444-4444-8444-444444444444";
const PINNED_VERSION_ID = "55555555-5555-4555-8555-555555555555";

const catalog: ProjectAssetList = {
  system_items: [
    {
      id: SYSTEM_ID,
      scope: "system",
      project_id: null,
      slug: "research-agent",
      display_name: "Research Agent",
      description:
        "Review, analyze, critique, and summarize academic papers with structured methodology assessment and constructive feedback.",
      status: "active",
      current_published_version_id: LATEST_VERSION_ID,
      version: 19,
      created_by_user_id: "system",
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-21T00:00:00Z",
      capabilities: [
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.manage_bindings",
      ],
      binding: {
        project_id: PROJECT_ID,
        kind: "agent",
        asset_id: SYSTEM_ID,
        version_id: PINNED_VERSION_ID,
        enabled: true,
        version: 7,
        created_by_user_id: "admin",
        updated_by_user_id: "admin",
        created_at: "2026-07-20T00:00:00Z",
        updated_at: "2026-07-21T00:00:00Z",
      },
    },
  ],
  project_items: [
    {
      id: PROJECT_ASSET_ID,
      scope: "project",
      project_id: PROJECT_ID,
      slug: "project-agent",
      display_name: "Project Agent",
      status: "active",
      current_published_version_id: null,
      version: 23,
      created_by_user_id: "editor",
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-21T00:00:00Z",
      capabilities: ["shared_assets.read", "shared_assets.edit"],
      binding: null,
    },
  ],
  request_id: "request-assets",
};

describe("project asset list", () => {
  test("hides system-provided Agents without removing other system asset sources", () => {
    expect(projectAssetSourceOptions("agents")).toEqual([
      ["project", "项目自建"],
    ]);
    expect(projectAssetSourceOptions("skills")).toEqual([
      ["system", "系统提供"],
      ["project", "项目自建"],
    ]);
    expect(projectAssetSourceOptions("mcp-servers")).toEqual([
      ["system", "系统提供"],
      ["project", "项目自建"],
    ]);
  });

  test("defaults to the useful source when one tab is empty", () => {
    expect(defaultProjectAssetSource(catalog)).toBe("project");
    expect(
      defaultProjectAssetSource({
        system_items: catalog.system_items,
        project_items: [],
      }),
    ).toBe("system");
    expect(
      defaultProjectAssetSource({ system_items: [], project_items: [] }),
    ).toBe("project");
  });

  test("filters only the active source tab by display name or slug", () => {
    expect(
      filterProjectAssetItems(catalog, "research", "system").map(
        (item) => item.id,
      ),
    ).toEqual([SYSTEM_ID]);
    expect(filterProjectAssetItems(catalog, "research", "project")).toEqual([]);

    expect(
      filterProjectAssetItems(catalog, "PROJECT-AGENT", "project").map(
        (item) => item.id,
      ),
    ).toEqual([PROJECT_ASSET_ID]);
  });

  test("renders one scoped panel without repeating the source label on every row", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="agents"
        data={catalog}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(html).toContain("Research Agent");
    expect(html).not.toContain("Project Agent");
    expect(html).not.toContain(">系统提供<");
    expect(html).not.toContain(">项目自建<");
    expect(html).toContain('role="switch"');
    expect(html).toContain("停用 Research Agent");
  });

  test("renders Skill descriptions with a direct binding switch while preserving detail access", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="skills"
        data={catalog}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(html).toContain(
      "Review, analyze, critique, and summarize academic papers",
    );
    expect(html).toContain("查看 Research Agent 详情");
    expect(html).toContain('role="switch"');
    expect(html).toContain("停用 Research Agent");
    expect(html).not.toContain("已有发布版本");
    expect(html).not.toContain(
      new Date(catalog.system_items[0]!.updated_at).toLocaleDateString("zh-CN"),
    );
  });

  test("does not present active project lifecycle as a fake enable control", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="agents"
        data={catalog}
        source="project"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(html).toContain("Project Agent");
    expect(html).toContain("尚未发布");
    expect(html).not.toContain('role="switch"');
    expect(html).not.toContain(">启用<");
  });

  test("derives safe system binding switch targets without changing lifecycle semantics", () => {
    expect(systemBindingToggleState(catalog.system_items[0]!)).toEqual({
      checked: true,
      disabled: false,
      targetVersionId: PINNED_VERSION_ID,
    });

    expect(
      systemBindingToggleState({
        ...catalog.system_items[0]!,
        binding: null,
      }),
    ).toEqual({
      checked: false,
      disabled: false,
      targetVersionId: LATEST_VERSION_ID,
    });

    expect(
      systemBindingToggleState({
        ...catalog.system_items[0]!,
        binding: null,
        current_published_version_id: null,
      }),
    ).toEqual({
      checked: false,
      disabled: true,
      targetVersionId: null,
    });

    expect(systemBindingToggleState(catalog.project_items[0]!)).toEqual({
      checked: false,
      disabled: true,
      targetVersionId: null,
    });
  });

  test("does not present optimistic revisions or UUID pointers as content versions", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="agents"
        data={catalog}
        source="system"
        selectedAssetId={SYSTEM_ID}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(html).not.toContain("资产版本");
    expect(html).not.toContain("绑定修订版本");
    expect(html).not.toContain(LATEST_VERSION_ID);
    expect(html).not.toContain(PINNED_VERSION_ID);
    expect(html).not.toContain(">19<");
    expect(html).not.toContain(">7<");
  });
});
