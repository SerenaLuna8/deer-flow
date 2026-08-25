import { expect, test } from "@rstest/core";

import {
  legacyCompatibleProjectAssetListSchema,
  projectAgentAssetListSchema,
  projectAssetListSchema,
} from "@/core/shared-assets";

const legacyItem = {
  id: "00000000-0000-4000-8000-000000000001",
  scope: "project",
  project_id: "00000000-0000-4000-8000-000000000002",
  slug: "legacy-item",
  display_name: "Legacy item",
  status: "active",
  current_published_version_id: "00000000-0000-4000-8000-000000000003",
  version: 2,
  created_by_user_id: "00000000-0000-4000-8000-000000000004",
  created_at: "2026-08-21T00:00:00Z",
  updated_at: "2026-08-21T00:00:00Z",
  capabilities: [],
  binding: null,
};

const response = {
  system_items: [],
  project_items: [legacyItem],
  request_id: "legacy-contract",
};

test("Agent and Skill lists reject removed lifecycle response aliases", () => {
  expect(projectAssetListSchema.safeParse(response).success).toBe(false);
});

test("Agent lists accept definition identity and reject Current Version identity", () => {
  const item = {
    ...legacyItem,
    slug: "definition-agent",
    display_name: "Definition Agent",
    definition_id: "00000000-0000-4000-8000-000000000005",
    revision: 3,
  } as Record<string, unknown>;
  delete item.current_published_version_id;
  delete item.version;
  const definitionResponse = {
    system_items: [],
    project_items: [item],
    request_id: "definition-contract",
  };

  expect(projectAgentAssetListSchema.parse(definitionResponse)).toEqual(
    definitionResponse,
  );
  expect(
    projectAgentAssetListSchema.safeParse({
      ...definitionResponse,
      project_items: [
        {
          ...item,
          current_version_id: item.definition_id,
        },
      ],
    }).success,
  ).toBe(false);
});

test("the isolated MCP compatibility schema still normalizes legacy fields", () => {
  const parsed = legacyCompatibleProjectAssetListSchema.parse(response);
  expect(parsed.project_items[0]).toMatchObject({
    current_version_id: legacyItem.current_published_version_id,
    revision: legacyItem.version,
  });
});
