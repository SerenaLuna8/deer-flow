import { describe, expect, test } from "@rstest/core";

import {
  ADMIN_ASSET_PAGE_SIZE,
  adminAssetCatalogPage,
  adminAssetCatalogSummary,
  adminCredentialPayloadGroupLabel,
  adminCredentialTypeLabel,
  adminMcpTransportLabel,
  clampAdminAssetPage,
  filterAdminProjectCatalogItems,
  filterAndSortAdminAssets,
  filterSystemAdminCatalogItems,
  resetAdminAssetPage,
} from "@/components/admin/assets/admin-asset-view-model";
import type { AssetSummary } from "@/core/shared-assets";

function asset(
  index: number,
  overrides: Partial<AssetSummary> = {},
): AssetSummary {
  const suffix = index.toString(16).padStart(12, "0");
  return {
    id: `00000000-0000-4000-8000-${suffix}`,
    scope: "system",
    project_id: null,
    slug: `skill-${index}`,
    display_name: `Skill ${index}`,
    status: "active",
    current_published_version_id: `10000000-0000-4000-8000-${suffix}`,
    version: 1,
    created_by_user_id: "system",
    created_at: "2026-07-01T00:00:00.000Z",
    updated_at: `2026-07-${String(Math.min(index, 28)).padStart(2, "0")}T00:00:00.000Z`,
    ...overrides,
  };
}

describe("admin asset catalog view model", () => {
  test("separates platform system assets from one project's governed assets", () => {
    const projectId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const otherProjectId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const systemAsset = asset(1);
    const projectAsset = asset(2, {
      scope: "project",
      project_id: projectId,
    });
    const otherProjectAsset = asset(3, {
      scope: "project",
      project_id: otherProjectId,
    });
    const malformedSystemAsset = asset(4, { project_id: projectId });
    const mixed = [
      systemAsset,
      projectAsset,
      otherProjectAsset,
      malformedSystemAsset,
    ];

    expect(filterSystemAdminCatalogItems(mixed)).toEqual([systemAsset]);
    expect(filterAdminProjectCatalogItems(mixed, projectId)).toEqual([
      projectAsset,
    ]);
  });

  test("localizes known Credential types and preserves unknown extension types", () => {
    const copy = {
      modelApiKey: "模型 API 密钥",
      apiKey: "API 密钥",
      token: "访问令牌",
      mcpAuth: "MCP 认证",
      oauth: "OAuth 授权",
      database: "数据库凭据",
    };

    expect(adminCredentialTypeLabel("model_api_key", copy)).toBe(
      "模型 API 密钥",
    );
    expect(adminCredentialTypeLabel("token", copy)).toBe("访问令牌");
    expect(adminCredentialTypeLabel("mcp_auth", copy)).toBe("MCP 认证");
    expect(adminCredentialTypeLabel("vendor_extension", copy)).toBe(
      "vendor_extension",
    );
  });

  test("localizes closed MCP transport and Credential payload groups while preserving technical codes", () => {
    const transportCopy = {
      stdio: "标准输入输出 (stdio)",
      sse: "服务器推送事件 (SSE)",
      http: "HTTP",
    };
    const payloadCopy = {
      env: "环境变量 (env)",
      headers: "请求头 (headers)",
      query: "查询参数 (query)",
      oauth: "OAuth (oauth)",
    };

    expect(adminMcpTransportLabel("stdio", transportCopy)).toBe(
      "标准输入输出 (stdio)",
    );
    expect(adminMcpTransportLabel("vendor", transportCopy)).toBe("vendor");
    expect(adminCredentialPayloadGroupLabel("headers", payloadCopy)).toBe(
      "请求头 (headers)",
    );
    expect(adminCredentialPayloadGroupLabel("query", payloadCopy)).toBe(
      "查询参数 (query)",
    );
    expect(adminCredentialPayloadGroupLabel("extension", payloadCopy)).toBe(
      "extension",
    );
  });

  test("summarizes real lifecycle, publication, and update fields", () => {
    const items = [
      asset(1, { status: "active", updated_at: "2026-07-01T00:00:00Z" }),
      asset(2, {
        status: "suspended",
        current_published_version_id: null,
        updated_at: "2026-07-05T12:00:00Z",
      }),
      asset(3, {
        status: "archived",
        current_published_version_id: null,
        updated_at: "2026-07-03T00:00:00Z",
      }),
    ];

    expect(adminAssetCatalogSummary(items)).toEqual({
      total: 3,
      active: 1,
      suspended: 1,
      archived: 1,
      unpublished: 2,
      latestUpdatedAt: "2026-07-05T12:00:00Z",
    });
    expect(adminAssetCatalogSummary([])).toEqual({
      total: 0,
      active: 0,
      suspended: 0,
      archived: 0,
      unpublished: 0,
      latestUpdatedAt: null,
    });
  });

  test("searches display name, slug, and id case-insensitively", () => {
    const items = [
      asset(1, {
        id: "aaaaaaaa-0000-4000-8000-000000000001",
        display_name: "Academic Review",
        slug: "paper-review",
      }),
      asset(2, {
        id: "bbbbbbbb-0000-4000-8000-000000000002",
        display_name: "ActWeave Docs",
        slug: "deerflow-docs",
      }),
    ];
    const filters = {
      status: "all",
      publication: "all",
      updatedSort: "newest",
    } as const;

    expect(
      filterAndSortAdminAssets(items, { ...filters, query: " ACADEMIC " }).map(
        (item) => item.slug,
      ),
    ).toEqual(["paper-review"]);
    expect(
      filterAndSortAdminAssets(items, {
        ...filters,
        query: "deerflow-docs",
      }).map((item) => item.slug),
    ).toEqual(["deerflow-docs"]);
    expect(
      filterAndSortAdminAssets(items, { ...filters, query: "BBBBBBBB" }).map(
        (item) => item.slug,
      ),
    ).toEqual(["deerflow-docs"]);
  });

  test("combines lifecycle and publication filters", () => {
    const items = [
      asset(1, { status: "active" }),
      asset(2, {
        status: "active",
        current_published_version_id: null,
      }),
      asset(3, {
        status: "suspended",
        current_published_version_id: null,
      }),
    ];

    expect(
      filterAndSortAdminAssets(items, {
        query: "",
        status: "active",
        publication: "unpublished",
        updatedSort: "newest",
      }).map((item) => item.id),
    ).toEqual([items[1]?.id]);
    expect(
      filterAndSortAdminAssets(items, {
        query: "",
        status: "all",
        publication: "published",
        updatedSort: "newest",
      }).map((item) => item.id),
    ).toEqual([items[0]?.id]);
  });

  test("sorts by update time in either direction and keeps ties stable", () => {
    const items = [
      asset(1, { updated_at: "2026-07-02T00:00:00Z" }),
      asset(2, { updated_at: "2026-07-03T00:00:00Z" }),
      asset(3, { updated_at: "2026-07-03T00:00:00Z" }),
      asset(4, { updated_at: "2026-07-01T00:00:00Z" }),
    ];
    const base = {
      query: "",
      status: "all",
      publication: "all",
    } as const;

    expect(
      filterAndSortAdminAssets(items, {
        ...base,
        updatedSort: "newest",
      }).map((item) => item.slug),
    ).toEqual(["skill-2", "skill-3", "skill-1", "skill-4"]);
    expect(
      filterAndSortAdminAssets(items, {
        ...base,
        updatedSort: "oldest",
      }).map((item) => item.slug),
    ).toEqual(["skill-4", "skill-1", "skill-2", "skill-3"]);
    expect(items.map((item) => item.slug)).toEqual([
      "skill-1",
      "skill-2",
      "skill-3",
      "skill-4",
    ]);
  });

  test("clamps pages and paginates twenty rows without losing order", () => {
    const items = Array.from({ length: 45 }, (_, index) => asset(index + 1));

    expect(ADMIN_ASSET_PAGE_SIZE).toBe(20);
    expect(clampAdminAssetPage(-3, items.length)).toBe(1);
    expect(clampAdminAssetPage(99, items.length)).toBe(3);
    expect(clampAdminAssetPage(Number.NaN, items.length)).toBe(1);
    expect(clampAdminAssetPage(4, 0)).toBe(1);

    const page = adminAssetCatalogPage(items, 99);
    expect(page.page).toBe(3);
    expect(page.totalPages).toBe(3);
    expect(page.totalItems).toBe(45);
    expect(page.items.map((item) => item.slug)).toEqual([
      "skill-41",
      "skill-42",
      "skill-43",
      "skill-44",
      "skill-45",
    ]);
  });

  test("resets only when a catalog input changes, otherwise clamps", () => {
    const current = {
      query: "",
      status: "all",
      publication: "all",
      updatedSort: "newest",
    } as const;

    expect(resetAdminAssetPage(2, current, current, 45)).toBe(2);
    expect(
      resetAdminAssetPage(
        2,
        current,
        { ...current, publication: "unpublished" },
        45,
      ),
    ).toBe(1);
    expect(resetAdminAssetPage(3, current, current, 5)).toBe(1);
  });
});
