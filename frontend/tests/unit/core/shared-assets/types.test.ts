import { describe, expect, test } from "@rstest/core";

import { CAPABILITIES, capabilitySchema } from "@/core/projects/types";
import {
  assetSummarySchema,
  assetVersionSchema,
  credentialMetadataSchema,
  systemBindingSchema,
} from "@/core/shared-assets/types";

const ASSET_ID = "11111111-1111-4111-8111-111111111111";
const VERSION_ID = "22222222-2222-4222-8222-222222222222";
const PROJECT_ID = "33333333-3333-4333-8333-333333333333";

describe("shared asset contracts", () => {
  test("strictly parses the Gateway asset item without inventing endpoint metadata", () => {
    const asset = assetSummarySchema.parse({
      id: ASSET_ID,
      scope: "project",
      project_id: PROJECT_ID,
      slug: "writer",
      display_name: "Writer",
      status: "active",
      current_published_version_id: VERSION_ID,
      version: 1,
      created_by_user_id: "user-1",
      created_at: "2026-07-14T00:00:00Z",
      updated_at: "2026-07-14T00:00:00Z",
    });

    expect(asset).not.toHaveProperty("capabilities");
    expect(asset).not.toHaveProperty("kind");
    expect(() =>
      assetSummarySchema.parse({ ...asset, role: "admin" }),
    ).toThrow();
  });

  test.each([
    "plaintext",
    "ciphertext",
    "nonce",
    "key_id",
    "storage_locator",
    "secret_hash",
  ])("rejects credential secret storage field %s", (field) => {
    expect(() =>
      credentialMetadataSchema.parse({
        id: ASSET_ID,
        scope: "project",
        project_id: PROJECT_ID,
        name: "github",
        display_name: "GitHub",
        credential_type: "token",
        status: "active",
        current_version_id: VERSION_ID,
        version: 1,
        created_by_user_id: "user-1",
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:00Z",
        [field]: "forbidden",
      }),
    ).toThrow();
  });

  test("strictly parses version and system binding metadata", () => {
    expect(
      assetVersionSchema.parse({
        id: VERSION_ID,
        asset_id: ASSET_ID,
        version_number: 1,
        workflow_status: "published",
        supersedes_version_id: null,
        created_at: "2026-07-14T00:00:00Z",
      }),
    ).toMatchObject({ version_number: 1, workflow_status: "published" });
    expect(
      systemBindingSchema.parse({
        project_id: PROJECT_ID,
        kind: "agent",
        asset_id: ASSET_ID,
        version_id: VERSION_ID,
        enabled: true,
        version: 1,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:00Z",
        request_id: "req-4",
      }),
    ).toMatchObject({ enabled: true, version: 1 });
  });

  test("declares the Gateway binding capability without deriving it from role", () => {
    expect(CAPABILITIES).toContain("shared_assets.manage_bindings");
    expect(capabilitySchema.parse("shared_assets.manage_bindings")).toBe(
      "shared_assets.manage_bindings",
    );
  });
});
