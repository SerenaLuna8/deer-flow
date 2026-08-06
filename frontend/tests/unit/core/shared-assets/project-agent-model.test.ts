import { expect, test } from "@rstest/core";

import { resolveThreadAgentModelRef } from "@/core/shared-assets/project-skill-catalog";
import type {
  ProjectAssetList,
  VersionHistoryResponse,
} from "@/core/shared-assets/types";

const AGENT_ID = "11111111-1111-4111-8111-111111111111";
const VERSION_ID = "22222222-2222-4222-8222-222222222222";

test("resolves an exact model from Thread Agent metadata and its published version", () => {
  const catalog = {
    project_items: [
      {
        id: AGENT_ID,
        scope: "project",
        current_published_version_id: VERSION_ID,
      },
    ],
    system_items: [],
  } as unknown as ProjectAssetList;
  const history = {
    data: [
      {
        id: VERSION_ID,
        agent_id: AGENT_ID,
        workflow_status: "published",
        model_ref: "deepseek-v4-flash",
      },
    ],
  } as unknown as VersionHistoryResponse;

  expect(
    resolveThreadAgentModelRef(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "project" },
      history,
    ),
  ).toBe("deepseek-v4-flash");
});

test("recognizes Main as default-bound without inventing an exact model", () => {
  const catalog = {
    project_items: [],
    system_items: [
      {
        id: AGENT_ID,
        scope: "system",
        slug: "project-assistant",
      },
    ],
  } as unknown as ProjectAssetList;

  expect(
    resolveThreadAgentModelRef(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "system" },
      undefined,
    ),
  ).toBe("default");
});

test("does not infer a model until exact Agent metadata and version agree", () => {
  const catalog = {
    project_items: [
      {
        id: AGENT_ID,
        scope: "project",
        current_published_version_id: VERSION_ID,
      },
    ],
    system_items: [],
  } as unknown as ProjectAssetList;

  expect(
    resolveThreadAgentModelRef(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "system" },
      undefined,
    ),
  ).toBeNull();
});

test("fails closed for a system Agent without an enabled version binding", () => {
  const catalog = {
    project_items: [],
    system_items: [
      {
        id: AGENT_ID,
        scope: "system",
        slug: "reviewer",
        binding: null,
      },
    ],
  } as unknown as ProjectAssetList;

  expect(
    resolveThreadAgentModelRef(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "system" },
      { data: [] } as unknown as VersionHistoryResponse,
    ),
  ).toBeNull();
});

test("fails closed when the selected Agent version is absent from history", () => {
  const catalog = {
    project_items: [
      {
        id: AGENT_ID,
        scope: "project",
        current_published_version_id: VERSION_ID,
      },
    ],
    system_items: [],
  } as unknown as ProjectAssetList;

  expect(
    resolveThreadAgentModelRef(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "project" },
      { data: [] } as unknown as VersionHistoryResponse,
    ),
  ).toBeNull();
});
