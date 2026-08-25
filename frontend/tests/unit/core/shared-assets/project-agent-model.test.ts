import { expect, test } from "@rstest/core";

import {
  isThreadProjectAgentArchived,
  resolveThreadAgentModelRef,
} from "@/core/shared-assets/project-skill-catalog";
import type {
  AgentDefinitionResponse,
  ProjectAssetList,
} from "@/core/shared-assets/types";

const AGENT_ID = "11111111-1111-4111-8111-111111111111";
const DEFINITION_ID = "22222222-2222-4222-8222-222222222222";

function definitionAggregate(
  modelRef = "deepseek-v4-flash",
): AgentDefinitionResponse {
  return {
    item: { id: AGENT_ID, definition_id: DEFINITION_ID, status: "active" },
    definition: {
      definition_id: DEFINITION_ID,
      agent_id: AGENT_ID,
      model_ref: modelRef,
    },
    request_id: "definition-request",
  } as AgentDefinitionResponse;
}

test("resolves an exact model from Thread Agent metadata and its Definition", () => {
  const catalog = {
    project_items: [
      {
        id: AGENT_ID,
        scope: "project",
        status: "active",
        definition_id: DEFINITION_ID,
      },
    ],
    system_items: [],
  } as unknown as ProjectAssetList;

  expect(
    resolveThreadAgentModelRef(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "project" },
      definitionAggregate(),
    ),
  ).toBe("deepseek-v4-flash");
});

test("recognizes Main as default-bound without inventing an exact model", () => {
  const catalog = {
    project_items: [],
    system_items: [
      { id: AGENT_ID, scope: "system", slug: "project-assistant" },
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

test("does not infer a model until exact Agent metadata and Definition agree", () => {
  const catalog = {
    project_items: [
      {
        id: AGENT_ID,
        scope: "project",
        status: "active",
        definition_id: DEFINITION_ID,
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

test("fails closed for a system Agent without an enabled Definition binding", () => {
  const catalog = {
    project_items: [],
    system_items: [
      {
        id: AGENT_ID,
        scope: "system",
        slug: "reviewer",
        status: "active",
        definition_id: DEFINITION_ID,
        binding: null,
      },
    ],
  } as unknown as ProjectAssetList;

  expect(
    resolveThreadAgentModelRef(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "system" },
      definitionAggregate(),
    ),
  ).toBeNull();
});

test("fails closed when the selected Agent Definition does not match the response", () => {
  const catalog = {
    project_items: [
      {
        id: AGENT_ID,
        scope: "project",
        status: "active",
        definition_id: DEFINITION_ID,
      },
    ],
    system_items: [],
  } as unknown as ProjectAssetList;
  const stale = definitionAggregate();
  stale.definition.definition_id = "33333333-3333-4333-8333-333333333333";

  expect(
    resolveThreadAgentModelRef(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "project" },
      stale,
    ),
  ).toBeNull();
});

test("fails closed when an existing Thread points at a suspended project Agent", () => {
  const catalog = {
    project_items: [
      {
        id: AGENT_ID,
        scope: "project",
        status: "suspended",
        definition_id: DEFINITION_ID,
      },
    ],
    system_items: [],
  } as unknown as ProjectAssetList;

  expect(
    resolveThreadAgentModelRef(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "project" },
      definitionAggregate(),
    ),
  ).toBeNull();
});

test("recognizes a project Agent removed from a settled catalog as archived", () => {
  const catalog = {
    project_items: [],
    system_items: [],
  } as unknown as ProjectAssetList;
  const metadata = { agent_asset_id: AGENT_ID, agent_scope: "project" };

  expect(isThreadProjectAgentArchived(catalog, metadata, true)).toBe(true);
  expect(isThreadProjectAgentArchived(catalog, metadata, false)).toBe(false);
  expect(isThreadProjectAgentArchived(undefined, metadata, true)).toBe(false);
});

test("does not mislabel system or still-cataloged project Agents as archived", () => {
  const catalog = {
    project_items: [
      {
        id: AGENT_ID,
        scope: "project",
        status: "suspended",
        definition_id: DEFINITION_ID,
      },
    ],
    system_items: [],
  } as unknown as ProjectAssetList;

  expect(
    isThreadProjectAgentArchived(
      catalog,
      { agent_asset_id: AGENT_ID, agent_scope: "project" },
      true,
    ),
  ).toBe(false);
  expect(
    isThreadProjectAgentArchived(
      { project_items: [], system_items: [] } as unknown as ProjectAssetList,
      { agent_asset_id: AGENT_ID, agent_scope: "system" },
      true,
    ),
  ).toBe(false);
});
