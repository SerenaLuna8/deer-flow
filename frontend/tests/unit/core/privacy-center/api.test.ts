import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  listPrivacyCases,
  privacyExportURL,
  requestPrivacyEarlyDelete,
} from "@/core/privacy-center/api";

const mockedFetch = rs.mocked(fetchWithAuth);
const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const JOB_ID = "33333333-3333-4333-8333-333333333333";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("privacy center api", () => {
  test("accepts the auth-disabled synthetic account as cache identity", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, []));

    await expect(listPrivacyCases("default")).resolves.toEqual([]);
    expect(mockedFetch).toHaveBeenCalledWith("/backend/api/privacy/cases", {
      credentials: "include",
      signal: undefined,
    });
  });

  test("uses account-keyed list request and forwards AbortSignal", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, [
        {
          project_id: PROJECT_ID,
          project_slug: "former-project",
          project_display_name: "Former project",
          project_icon: "folder",
          membership_status: "left",
          retention_kind: "former_owner",
          deletion_deadline: "2026-08-21T08:00:00Z",
          early_delete_requested: false,
        },
      ]),
    );
    const signal = new AbortController().signal;

    const result = await listPrivacyCases(ACCOUNT_ID, signal);

    expect(result).toHaveLength(1);
    expect(mockedFetch).toHaveBeenCalledWith("/backend/api/privacy/cases", {
      credentials: "include",
      signal,
    });
  });

  test("strictly rejects unknown private or authority fields", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, [
        {
          project_id: PROJECT_ID,
          project_slug: "former-project",
          project_display_name: "Former project",
          project_icon: "folder",
          membership_status: "left",
          retention_kind: "former_owner",
          deletion_deadline: "2026-08-21T08:00:00Z",
          early_delete_requested: false,
          owner_user_id: ACCOUNT_ID,
        },
      ]),
    );

    await expect(listPrivacyCases(ACCOUNT_ID)).rejects.toMatchObject({
      code: "PRIVACY_RESPONSE_INVALID",
    });
  });

  test("builds a direct streaming download URL without fetching export body", () => {
    const result = privacyExportURL(ACCOUNT_ID, PROJECT_ID);

    expect(result).toBe(`/backend/api/privacy/cases/${PROJECT_ID}/export`);
    expect(mockedFetch).not.toHaveBeenCalled();
  });

  test("admits early delete through the durable job endpoint", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(202, {
        project_id: PROJECT_ID,
        job_id: JOB_ID,
        status: "queued",
      }),
    );
    const signal = new AbortController().signal;

    const result = await requestPrivacyEarlyDelete(
      ACCOUNT_ID,
      PROJECT_ID,
      signal,
    );

    expect(result.job_id).toBe(JOB_ID);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/privacy/cases/${PROJECT_ID}/early-delete`,
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        signal,
      }),
    );
  });
});
