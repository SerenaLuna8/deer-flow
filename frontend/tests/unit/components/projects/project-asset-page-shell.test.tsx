import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  filterProjectAssetList,
  ProjectAssetListView,
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
  test("filters locally by display name or slug without collapsing source groups", () => {
    const byName = filterProjectAssetList(catalog, "research", "all");
    expect(byName.system_items.map((item) => item.id)).toEqual([SYSTEM_ID]);
    expect(byName.project_items).toEqual([]);

    const bySlug = filterProjectAssetList(catalog, "PROJECT-AGENT", "all");
    expect(bySlug.system_items).toEqual([]);
    expect(bySlug.project_items.map((item) => item.id)).toEqual([
      PROJECT_ASSET_ID,
    ]);
  });

  test("filters by source and returns both empty groups for an unmatched search", () => {
    const projectOnly = filterProjectAssetList(catalog, "", "project");
    expect(projectOnly.system_items).toEqual([]);
    expect(projectOnly.project_items.map((item) => item.id)).toEqual([
      PROJECT_ASSET_ID,
    ]);

    const empty = filterProjectAssetList(catalog, "not-found", "all");
    expect(empty.system_items).toEqual([]);
    expect(empty.project_items).toEqual([]);
    expect(empty.request_id).toBe(catalog.request_id);

    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="agents"
        data={empty}
        selectedAssetId={null}
        onSelect={() => undefined}
      />,
    );
    expect(html).toContain("系统提供");
    expect(html).toContain("项目自建");
  });

  test("lists concrete assets and translates binding state into project language", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="agents"
        data={catalog}
        selectedAssetId={null}
        onSelect={() => undefined}
      />,
    );

    expect(html).toContain("Research Agent");
    expect(html).toContain("Project Agent");
    expect(html).toContain("系统提供");
    expect(html).toContain("项目自建");
    expect(html).toContain("有新版本");
    expect(html).toContain("尚未发布");
  });

  test("does not present optimistic revisions or UUID pointers as content versions", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="agents"
        data={catalog}
        selectedAssetId={SYSTEM_ID}
        onSelect={() => undefined}
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
