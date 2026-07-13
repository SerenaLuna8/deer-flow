import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  SharedAssetApiError,
  createProjectAsset,
  listAdminAssets,
  listProjectAssets,
} from "@/core/shared-assets/api";

const mockedFetch = rs.mocked(fetchWithAuth);
const PROJECT_ID = "33333333-3333-4333-8333-333333333333";
const asset = {
  id: "11111111-1111-4111-8111-111111111111",
  scope: "project",
  project_id: PROJECT_ID,
  slug: "writer",
  display_name: "Writer",
  status: "active",
  current_published_version_id: null,
  version: 1,
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:00:00Z",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("shared asset api", () => {
  test("uses only the authenticated fetcher for project and admin lists", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          system_items: [],
          project_items: [asset],
          request_id: "req-1",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          items: [{ ...asset, scope: "system", project_id: null }],
          request_id: "req-2",
        }),
      );
    const signal = new AbortController().signal;

    await expect(
      listProjectAssets(PROJECT_ID, "agents", signal),
    ).resolves.toMatchObject({ project_items: [asset] });
    await expect(listAdminAssets("agents", signal)).resolves.toMatchObject({
      items: [{ id: asset.id }],
    });
    expect(mockedFetch.mock.calls).toEqual([
      [`/backend/api/projects/${PROJECT_ID}/agents`, { signal }],
      ["/backend/api/admin/assets/agents", { signal }],
    ]);
  });

  test("validates mutation input before sending it through the authenticated fetcher", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(201, { item: asset, request_id: "req-3" }),
    );
    await createProjectAsset(PROJECT_ID, "agents", {
      slug: "writer",
      display_name: "Writer",
    });
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/agents`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ slug: "writer", display_name: "Writer" }),
      }),
    );
    await expect(
      createProjectAsset("not-a-uuid", "agents", {
        slug: "writer",
        display_name: "Writer",
      }),
    ).rejects.toMatchObject({ code: "ASSET_VALIDATION_FAILED" });
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  test("uses canonical public errors and rejects unsafe responses", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(403, {
        detail: {
          code: "asset_forbidden",
          message: "SQL secret should not escape",
          request_id: "req-4",
        },
      }),
    );
    const error = await listAdminAssets("skills").catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(SharedAssetApiError);
    expect(error).toMatchObject({
      status: 403,
      code: "ASSET_FORBIDDEN",
      message: "Asset capability required",
    });

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        items: [
          {
            ...asset,
            scope: "system",
            project_id: null,
            plaintext: "forbidden",
          },
        ],
        request_id: "req-5",
      }),
    );
    await expect(listAdminAssets("agents")).rejects.toMatchObject({
      code: "ASSET_RESPONSE_INVALID",
      message: "Shared asset response was invalid",
    });
  });

  test("maps explicit authentication failures and preserves aborts", async () => {
    mockedFetch.mockRejectedValueOnce(new AuthRequiredError());
    await expect(listAdminAssets("agents")).rejects.toMatchObject({
      status: 401,
      code: "AUTH_REQUIRED",
      message: "Authentication required",
    });

    const aborted = new DOMException("Aborted", "AbortError");
    mockedFetch.mockRejectedValueOnce(aborted);
    await expect(listAdminAssets("agents")).rejects.toBe(aborted);
  });
});
