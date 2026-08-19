import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  filterProjectAssetItems,
  ProjectAgentDirectorySearch,
  projectAssetSelectionDecision,
  projectAssetSelectionFromSearch,
  projectAssetSelectionHref,
} from "@/components/projects/assets/project-asset-page-shell";
import type { ProjectAssetItem, ProjectAssetList } from "@/core/shared-assets";

const AGENT_A_ID = "00000000-0000-4000-8000-000000000010";
const AGENT_B_ID = "00000000-0000-4000-8000-000000000020";

function agent(
  id: string,
  scope: "system" | "project",
  displayName: string,
  slug: string,
): ProjectAssetItem {
  return {
    id,
    scope,
    project_id:
      scope === "project" ? "00000000-0000-4000-8000-000000000001" : null,
    slug,
    display_name: displayName,
    description: null,
    status: "active",
    current_published_version_id: null,
    version: 1,
    created_by_user_id: "00000000-0000-4000-8000-000000000002",
    created_at: "2026-08-13T00:00:00Z",
    updated_at: "2026-08-13T00:00:00Z",
    capabilities: [],
    binding: null,
  };
}

const catalog = {
  system_items: [agent(AGENT_A_ID, "system", "Main", "project-assistant")],
  project_items: [
    agent(AGENT_B_ID, "project", "Code Reviewer", "code-reviewer"),
  ],
} as ProjectAssetList;

describe("Agent directory search and deep-link navigation", () => {
  test("renders an accessible Agent search control with result feedback", () => {
    const html = renderToStaticMarkup(
      <ProjectAgentDirectorySearch
        searchQuery="review"
        visibleCount={1}
        sourceCount={2}
        onSearchQueryChange={() => undefined}
      />,
    );

    expect(html).toContain("搜索 Agent");
    expect(html).toContain('type="search"');
    expect(html).toContain('placeholder="搜索名称或 slug"');
    expect(html).toContain('role="status"');
    expect(html).toContain("显示 1 / 2 项");
  });

  test("filters both system and project Agent collections by name or slug", () => {
    expect(
      filterProjectAssetItems(catalog, "PROJECT-assistant", "system").map(
        (item) => item.id,
      ),
    ).toEqual([AGENT_A_ID]);
    expect(
      filterProjectAssetItems(catalog, "code reviewer", "project").map(
        (item) => item.id,
      ),
    ).toEqual([AGENT_B_ID]);
    expect(filterProjectAssetItems(catalog, "missing", "project")).toEqual([]);
  });

  test("adds and removes only agent_id while preserving other query state", () => {
    const pathname = "/projects/alpha/agents";
    const opened = projectAssetSelectionHref(
      pathname,
      "intent=start_chat&intent_id=request-1&tab=available",
      "agent_id",
      AGENT_B_ID,
    );

    expect(opened).toBe(
      `${pathname}?intent=start_chat&intent_id=request-1&tab=available&agent_id=${AGENT_B_ID}`,
    );
    expect(
      projectAssetSelectionHref(
        pathname,
        opened.split("?")[1] ?? "",
        "agent_id",
        null,
      ),
    ).toBe(`${pathname}?intent=start_chat&intent_id=request-1&tab=available`);
  });

  test("replaces an existing agent_id instead of duplicating it", () => {
    expect(
      projectAssetSelectionHref(
        "/projects/alpha/agents",
        `agent_id=${AGENT_A_ID}&agent_id=${AGENT_B_ID}&intent=start_chat`,
        "agent_id",
        AGENT_B_ID,
      ),
    ).toBe(`/projects/alpha/agents?agent_id=${AGENT_B_ID}&intent=start_chat`);
  });

  test("clears selection-dependent deep-link intent when another asset is selected", () => {
    expect(
      projectAssetSelectionHref(
        "/projects/alpha/skills",
        `skill_id=${AGENT_A_ID}&skill_version_id=${AGENT_B_ID}&configure_credentials=1&tab=project`,
        "skill_id",
        AGENT_B_ID,
        ["skill_version_id", "configure_credentials"],
      ),
    ).toBe(`/projects/alpha/skills?skill_id=${AGENT_B_ID}&tab=project`);
  });

  test("reads the selected Agent from browser history query state", () => {
    expect(
      projectAssetSelectionFromSearch(
        `?intent=start_chat&agent_id=${AGENT_A_ID}`,
        "agent_id",
      ),
    ).toBe(AGENT_A_ID);
    expect(
      projectAssetSelectionFromSearch("?intent=start_chat", "agent_id"),
    ).toBeNull();
    expect(
      projectAssetSelectionFromSearch(
        `?agent_id=${AGENT_A_ID}&agent_id=${AGENT_B_ID}`,
        "agent_id",
      ),
    ).toBeNull();
    expect(
      projectAssetSelectionFromSearch("?agent_id=not-a-uuid", "agent_id"),
    ).toBeNull();
  });

  test("requires discard confirmation before dirty card or history selection changes", () => {
    expect(projectAssetSelectionDecision(AGENT_A_ID, AGENT_B_ID, true)).toBe(
      "confirm-discard",
    );
    expect(projectAssetSelectionDecision(AGENT_A_ID, null, true)).toBe(
      "confirm-discard",
    );
    expect(projectAssetSelectionDecision(AGENT_A_ID, AGENT_A_ID, true)).toBe(
      "unchanged",
    );
    expect(projectAssetSelectionDecision(AGENT_A_ID, AGENT_B_ID, false)).toBe(
      "apply",
    );
  });
});
