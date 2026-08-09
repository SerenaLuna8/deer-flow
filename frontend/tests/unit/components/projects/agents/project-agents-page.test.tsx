import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectAgentCatalogView,
  sortProjectAgentsWithDefaultFirst,
} from "@/components/projects/assets/project-agents-page";
import type { Capability } from "@/core/projects/types";
import type { ProjectAssetItem } from "@/core/shared-assets";

const PROJECT_ID = "00000000-0000-4000-8000-000000000001";
const MAIN_ID = "00000000-0000-4000-8000-000000000010";
const DEFAULT_ID = "00000000-0000-4000-8000-000000000020";
const OTHER_ID = "00000000-0000-4000-8000-000000000030";
const VERSION_ID = "00000000-0000-4000-8000-000000000040";

const PROJECT_CAPABILITIES = [
  "private_work.create",
  "shared_assets.execute",
  "shared_assets.manage_bindings",
] satisfies Capability[];

function agent(
  overrides: Partial<ProjectAssetItem> &
    Pick<ProjectAssetItem, "id" | "scope" | "slug" | "display_name">,
): ProjectAssetItem {
  return {
    project_id: overrides.scope === "project" ? PROJECT_ID : null,
    status: "active",
    current_published_version_id: VERSION_ID,
    version: 1,
    created_by_user_id: "user-1",
    created_at: "2026-08-09T00:00:00Z",
    updated_at: "2026-08-09T00:00:00Z",
    capabilities: PROJECT_CAPABILITIES,
    binding: null,
    description: null,
    ...overrides,
  };
}

const main = agent({
  id: MAIN_ID,
  scope: "system",
  slug: "project-assistant",
  display_name: "Main",
  description: "系统内置的通用 Agent，适用于大多数对话与协作场景。",
});
const codeReviewer = agent({
  id: DEFAULT_ID,
  scope: "project",
  slug: "code-reviewer",
  display_name: "code-reviewer",
  description: "代码审查、代码质量、Bug 与安全审查",
});
const sequentialReviewer = agent({
  id: OTHER_ID,
  scope: "project",
  slug: "sequential-reviewer",
  display_name: "sequential-reviewer",
  description: "审查 Python 后端服务代码并输出可执行的审查报告。",
});

function renderCatalog(viewMode: "cards" | "list") {
  return renderToStaticMarkup(
    <ProjectAgentCatalogView
      systemItems={[main]}
      projectItems={[sequentialReviewer, codeReviewer]}
      projectCapabilities={PROJECT_CAPABILITIES}
      viewMode={viewMode}
      selectedAssetId={null}
      creatingChatForAgentId={null}
      defaultAgentId={DEFAULT_ID}
      onSelect={() => undefined}
      onStartChat={() => undefined}
      onSetDefault={() => undefined}
      onSetMainDefault={() => undefined}
    />,
  );
}

describe("ProjectAgentCatalogView", () => {
  test("uses one view mode for both system and project sections", () => {
    const cardHtml = renderCatalog("cards");
    const listHtml = renderCatalog("list");

    expect(cardHtml.match(/data-agent-view="cards"/gu)).toHaveLength(2);
    expect(cardHtml).not.toContain('data-agent-view="list"');
    expect(listHtml.match(/data-agent-view="list"/gu)).toHaveLength(2);
    expect(listHtml).not.toContain('data-agent-view="cards"');
  });

  test("keeps card mode compact for dense desktop catalogs", () => {
    const html = renderCatalog("cards");

    expect(html.match(/xl:grid-cols-3/gu)).toHaveLength(2);
    expect(html).not.toContain("min-h-52");
    expect(html).not.toContain("min-h-64");
    expect(html.match(/line-clamp-2 h-10 overflow-hidden/gu)).toHaveLength(3);
    expect(html).not.toContain("line-clamp-2 block");
    expect(html).not.toContain("w-full flex-1 px-5 pt-3 pb-4");
    expect(html.match(/w-full px-5 pt-3 pb-3/gu)).toHaveLength(3);
  });

  test("orders the project default first and never offers a restore-Main action", () => {
    const html = renderCatalog("cards");

    expect(html.indexOf("code-reviewer")).toBeLessThan(
      html.indexOf("sequential-reviewer"),
    );
    expect(html).toContain("当前默认");
    expect(html).not.toContain("恢复 Main");
    expect(html).not.toContain("将 code-reviewer 设为默认");
    expect(html).toContain("将 Main 设为默认");
    expect(html).toContain("将 sequential-reviewer 设为默认");
  });
});

test("sortProjectAgentsWithDefaultFirst preserves stable order after the default", () => {
  expect(
    sortProjectAgentsWithDefaultFirst(
      [sequentialReviewer, codeReviewer, main],
      DEFAULT_ID,
    ).map((item) => item.id),
  ).toEqual([DEFAULT_ID, OTHER_ID, MAIN_ID]);
});
