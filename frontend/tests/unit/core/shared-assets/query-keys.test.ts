import { describe, expect, test } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import {
  invalidateAdminAssetQueries,
  invalidateAdminProjectAssetQueries,
  invalidateProjectAssetQueries,
} from "@/core/shared-assets/hooks";
import {
  adminAssetKey,
  adminProjectAssetKey,
  adminProjectAssetVersionsKey,
  projectAssetKey,
  projectAssetVersionsKey,
  projectMcpEditableConfigurationKey,
  projectSkillVersionFileKey,
} from "@/core/shared-assets/query-keys";

const ADMIN_PROJECT_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_ADMIN_PROJECT_ID = "44444444-4444-4444-8444-444444444444";
const ADMIN_ASSET_ID = "11111111-1111-4111-8111-111111111111";

describe("shared asset query isolation", () => {
  test("keys always include account and project keys also include project and kind", () => {
    expect(adminAssetKey("u1", "agents")).toEqual([
      "account",
      "u1",
      "shared-assets",
      "admin",
      "agents",
    ]);
    expect(projectAssetKey("u1", "p1", "agents")).toEqual([
      "account",
      "u1",
      "shared-assets",
      "project",
      "p1",
      "agents",
    ]);
    expect(projectAssetKey("u1", "p1", "agents")).not.toEqual(
      projectAssetKey("u2", "p1", "agents"),
    );
    expect(projectAssetKey("u1", "p1", "agents")).not.toEqual(
      projectAssetKey("u1", "p2", "agents"),
    );
    expect(projectAssetKey("u1", "p1", "agents")).not.toEqual(
      projectAssetKey("u1", "p1", "skills"),
    );
  });

  test("skill file content keys isolate account, project, asset, version, and path", () => {
    const key = projectSkillVersionFileKey(
      "u1",
      "p1",
      "asset-1",
      "version-1",
      "references/guide.md",
    );
    expect(key).toEqual([
      "account",
      "u1",
      "shared-assets",
      "project",
      "p1",
      "skills",
      "asset",
      "asset-1",
      "versions",
      "version",
      "version-1",
      "file",
      "references/guide.md",
    ]);
    expect(key).not.toEqual(
      projectSkillVersionFileKey(
        "u1",
        "p1",
        "asset-1",
        "version-2",
        "references/guide.md",
      ),
    );
  });

  test("editable MCP configuration has an exact cache isolated from redacted history", () => {
    const key = projectMcpEditableConfigurationKey(
      "u1",
      ADMIN_PROJECT_ID,
      ADMIN_ASSET_ID,
    );
    expect(key).toEqual([
      "account",
      "u1",
      "shared-assets",
      "project",
      ADMIN_PROJECT_ID,
      "mcp-servers",
      "asset",
      ADMIN_ASSET_ID,
      "editable-configuration",
    ]);
    expect(key).not.toEqual(
      projectAssetVersionsKey(
        "u1",
        ADMIN_PROJECT_ID,
        "mcp-servers",
        ADMIN_ASSET_ID,
      ),
    );
  });

  test("project invalidation touches only the current account project and kind", async () => {
    const client = new QueryClient();
    const target = projectAssetKey("u1", "p1", "agents");
    const targetVersions = projectAssetVersionsKey(
      "u1",
      "p1",
      "agents",
      "asset-1",
    );
    const untouched = [
      projectAssetKey("u2", "p1", "agents"),
      projectAssetKey("u1", "p2", "agents"),
      projectAssetKey("u1", "p1", "skills"),
    ] as const;
    client.setQueryData(target, "target");
    client.setQueryData(targetVersions, "target versions");
    for (const key of untouched) client.setQueryData(key, "untouched");

    await invalidateProjectAssetQueries(client, "u1", "p1", "agents");

    expect(client.getQueryState(target)?.isInvalidated).toBe(true);
    expect(client.getQueryState(targetVersions)?.isInvalidated).toBe(true);
    for (const key of untouched) {
      expect(client.getQueryState(key)?.isInvalidated).toBe(false);
    }
  });

  test("admin invalidation does not clear another account or kind", async () => {
    const client = new QueryClient();
    const target = adminAssetKey("u1", "credentials");
    const otherAccount = adminAssetKey("u2", "credentials");
    const otherKind = adminAssetKey("u1", "agents");
    for (const key of [target, otherAccount, otherKind]) {
      client.setQueryData(key, "cached");
    }

    await invalidateAdminAssetQueries(client, "u1", "credentials");

    expect(client.getQueryState(target)?.isInvalidated).toBe(true);
    expect(client.getQueryState(otherAccount)?.isInvalidated).toBe(false);
    expect(client.getQueryState(otherKind)?.isInvalidated).toBe(false);
  });

  test("admin project override keys cannot collide with global admin or member project caches", async () => {
    const client = new QueryClient();
    const target = adminProjectAssetKey("u1", ADMIN_PROJECT_ID, "agents");
    const targetVersions = adminProjectAssetVersionsKey(
      "u1",
      ADMIN_PROJECT_ID,
      "agents",
      ADMIN_ASSET_ID,
    );
    const untouched = [
      adminAssetKey("u1", "agents"),
      projectAssetKey("u1", ADMIN_PROJECT_ID, "agents"),
      adminProjectAssetKey("u2", ADMIN_PROJECT_ID, "agents"),
      adminProjectAssetKey("u1", OTHER_ADMIN_PROJECT_ID, "agents"),
      adminProjectAssetKey("u1", ADMIN_PROJECT_ID, "skills"),
    ] as const;
    client.setQueryData(target, "target");
    client.setQueryData(targetVersions, "target versions");
    for (const key of untouched) client.setQueryData(key, "untouched");

    await invalidateAdminProjectAssetQueries(
      client,
      "u1",
      ADMIN_PROJECT_ID,
      "agents",
    );

    expect(client.getQueryState(target)?.isInvalidated).toBe(true);
    expect(client.getQueryState(targetVersions)?.isInvalidated).toBe(true);
    for (const key of untouched) {
      expect(client.getQueryState(key)?.isInvalidated).toBe(false);
    }
  });
});
