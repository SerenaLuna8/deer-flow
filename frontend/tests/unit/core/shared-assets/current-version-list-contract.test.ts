import { expect, test } from "@rstest/core";

import {
  legacyCompatibleProjectAssetListSchema,
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

test("the isolated MCP compatibility schema still normalizes legacy fields", () => {
  const parsed = legacyCompatibleProjectAssetListSchema.parse(response);
  expect(parsed.project_items[0]).toMatchObject({
    current_version_id: legacyItem.current_published_version_id,
    revision: legacyItem.version,
  });
});
