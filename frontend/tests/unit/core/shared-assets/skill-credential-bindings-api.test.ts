import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  getProjectSkillCredentialBindings,
  updateProjectSkillCredentialBindings,
} from "@/core/shared-assets/api";

const mockedFetch = rs.mocked(fetchWithAuth);
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const SKILL_ID = "33333333-3333-4333-8333-333333333333";
const SKILL_VERSION_ID = "44444444-4444-4444-8444-444444444444";
const CREDENTIAL_ID = "55555555-5555-4555-8555-555555555555";
const CREDENTIAL_VERSION_ID = "66666666-6666-4666-8666-666666666666";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const response = {
  skill_id: SKILL_ID,
  skill_version_id: SKILL_VERSION_ID,
  revision: 1,
  requirements: [
    {
      name: "WEATHER_API_KEY",
      optional: false,
      configured: false,
      credential_id: null,
      credential_version_id: null,
      credential_display_name: null,
      credential_version_number: null,
      eligible_credentials: [
        {
          credential_id: CREDENTIAL_ID,
          credential_version_id: CREDENTIAL_VERSION_ID,
          display_name: "Weather production",
          version_number: 1,
        },
      ],
    },
  ],
  request_id: "request-bindings",
};

describe("Skill Credential binding API", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
  });

  test("forwards AbortSignal on GET and sends only version references on PUT", async () => {
    mockedFetch
      .mockResolvedValueOnce(jsonResponse(response))
      .mockResolvedValueOnce(
        jsonResponse({
          ...response,
          revision: 2,
          requirements: [
            {
              ...response.requirements[0],
              configured: true,
              credential_id: CREDENTIAL_ID,
              credential_version_id: CREDENTIAL_VERSION_ID,
              credential_display_name: "Weather production",
              credential_version_number: 1,
            },
          ],
        }),
      );
    const signal = new AbortController().signal;

    await expect(
      getProjectSkillCredentialBindings(PROJECT_ID, SKILL_ID, signal),
    ).resolves.toEqual(response);
    await updateProjectSkillCredentialBindings(
      PROJECT_ID,
      SKILL_ID,
      {
        expected_revision: 1,
        bindings: [
          {
            name: "WEATHER_API_KEY",
            credential_version_id: CREDENTIAL_VERSION_ID,
          },
        ],
      },
      signal,
    );

    const url = `/backend/api/projects/${PROJECT_ID}/skills/${SKILL_ID}/credential-bindings`;
    expect(mockedFetch.mock.calls[0]).toEqual([url, { signal }]);
    expect(mockedFetch.mock.calls[1]).toEqual([
      url,
      expect.objectContaining({
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision: 1,
          bindings: [
            {
              name: "WEATHER_API_KEY",
              credential_version_id: CREDENTIAL_VERSION_ID,
            },
          ],
        }),
        signal,
      }),
    ]);
    expect(mockedFetch.mock.calls[1]?.[1]).not.toHaveProperty("secret");
    expect(mockedFetch.mock.calls[1]?.[1]).not.toHaveProperty("payload");
  });
});
