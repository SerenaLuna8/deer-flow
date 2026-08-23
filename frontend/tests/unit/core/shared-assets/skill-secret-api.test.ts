import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  activateProjectAssetVersion,
  clearProjectSkillSecret,
  getProjectSkillActivationReadiness,
  getProjectSkillSecrets,
  replaceProjectSkillSecrets,
} from "@/core/shared-assets";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const SKILL_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_ID = "33333333-3333-4333-8333-333333333333";
const SHA = "a".repeat(64);

afterEach(() => {
  rs.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200) {
  return Response.json(body, { status });
}

const secretStatus = {
  skill_id: SKILL_ID,
  skill_version_id: VERSION_ID,
  revision: 2,
  readiness: "ready" as const,
  requirements: [
    {
      name: "API_KEY",
      target_env: "API_KEY",
      optional: false,
      configured: true,
      revision: 2,
    },
  ],
  request_id: "secret-status",
};

describe("Skill domain secret API", () => {
  test("reads status only and writes values to the exact Skill version", async () => {
    const fetchMock = rs
      .fn()
      .mockResolvedValueOnce(jsonResponse(secretStatus))
      .mockResolvedValueOnce(jsonResponse(secretStatus));
    rs.stubGlobal("fetch", fetchMock);

    await expect(
      getProjectSkillSecrets(PROJECT_ID, SKILL_ID, VERSION_ID),
    ).resolves.toEqual(secretStatus);
    await expect(
      replaceProjectSkillSecrets(PROJECT_ID, SKILL_ID, VERSION_ID, {
        secrets: { API_KEY: "temporary-value" },
      }),
    ).resolves.toEqual(secretStatus);

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining(
        `/api/projects/${PROJECT_ID}/skills/${SKILL_ID}/versions/${VERSION_ID}/secrets`,
      ),
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ secrets: { API_KEY: "temporary-value" } }),
      }),
    );
    expect(secretStatus).not.toHaveProperty("secrets");
  });

  test("clears one exact secret only with explicit confirmation", async () => {
    const cleared = {
      ...secretStatus,
      readiness: "unready" as const,
      requirements: [
        {
          name: "API_KEY",
          target_env: "API_KEY",
          optional: false,
          configured: false,
          revision: 3,
        },
      ],
    };
    const fetchMock = rs.fn(async () => jsonResponse(cleared));
    rs.stubGlobal("fetch", fetchMock);
    await expect(
      clearProjectSkillSecret(PROJECT_ID, SKILL_ID, VERSION_ID, "API_KEY", {
        confirmed: true,
      }),
    ).resolves.toEqual(cleared);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(`/secrets/API_KEY/clear`),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ confirmed: true }),
      }),
    );
  });

  test("activation pins the secret revision returned by readiness", async () => {
    const plan = {
      skill_id: SKILL_ID,
      skill_version_id: VERSION_ID,
      revision: 4,
      payload_checksum: SHA,
      secret_revision: 2,
      secrets_autonomous: false,
      ready: true,
      required_count: 1,
      configured_required_count: 1,
      requirements: [
        {
          name: "API_KEY",
          target_env: "API_KEY",
          optional: false,
          configured: true,
        },
      ],
      request_id: "readiness",
    };
    const activation = {
      data: {
        id: VERSION_ID,
        skill_id: SKILL_ID,
        version_number: 2,
        relation: "current",
        description: "Example",
        frontmatter: {},
        compatibility: null,
        secret_requirements: [
          { name: "API_KEY", target_env: "API_KEY", optional: false },
        ],
        scan_decision: "allow",
        scan_rule_ids: [],
        scan_summary: {},
        file_views: [],
        supersedes_version_id: null,
        payload_checksum: SHA,
        revoked_at: null,
        revoked_by_user_id: null,
        revocation_reason_code: null,
        governance_status: "active",
        binding_eligible: true,
        created_by_user_id: "system",
        created_at: "2026-08-22T00:00:00Z",
      },
      request_id: "activation",
    };
    const fetchMock = rs
      .fn()
      .mockResolvedValueOnce(jsonResponse(plan))
      .mockResolvedValueOnce(jsonResponse(activation));
    rs.stubGlobal("fetch", fetchMock);
    await expect(
      getProjectSkillActivationReadiness(PROJECT_ID, SKILL_ID, VERSION_ID),
    ).resolves.toEqual(plan);
    const input = {
      expected_revision: 4,
      expected_payload_checksum: SHA,
      expected_secret_revision: 2,
    };
    await activateProjectAssetVersion(
      PROJECT_ID,
      "skills",
      SKILL_ID,
      VERSION_ID,
      input,
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining(`/versions/${VERSION_ID}/activate`),
      expect.objectContaining({ body: JSON.stringify(input) }),
    );
  });
});
