import { describe, expect, test } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import {
  invalidateAdminAssetQueries,
  invalidateProjectAssetQueries,
} from "@/core/shared-assets/hooks";
import {
  adminAssetKey,
  projectAssetKey,
  projectAssetVersionsKey,
} from "@/core/shared-assets/query-keys";

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
});
