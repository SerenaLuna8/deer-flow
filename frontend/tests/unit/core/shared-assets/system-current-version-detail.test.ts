import { afterEach, describe, expect, rs, test } from "@rstest/core";

import { listSystemAssetVersions } from "@/core/shared-assets";

const SKILL_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_ID = "33333333-3333-4333-8333-333333333333";
const USER_ID = "44444444-4444-4444-8444-444444444444";

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("System Current Version detail API", () => {
  test("loads a System Skill through the global read-only catalog", async () => {
    const fetchMock = rs.fn(
      async () =>
        new Response(
          JSON.stringify({
            data: [
              {
                id: VERSION_ID,
                skill_id: SKILL_ID,
                version_number: 1,
                relation: "current",
                description: "System Skill",
                frontmatter: { name: "system-skill" },
                compatibility: null,
                secret_requirements: [],
                file_views: [],
                supersedes_version_id: null,
                payload_checksum: "sha256:payload",
                revoked_at: null,
                revoked_by_user_id: null,
                revocation_reason_code: null,
                governance_status: "active",
                binding_eligible: true,
                created_by_user_id: USER_ID,
                created_at: "2026-08-21T00:00:00Z",
              },
            ],
            request_id: "system-current-detail",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    );
    rs.stubGlobal("fetch", fetchMock);

    await expect(
      listSystemAssetVersions("skills", SKILL_ID),
    ).resolves.toMatchObject({
      data: [{ id: VERSION_ID, relation: "current" }],
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(
        `/api/assets/catalog/skills/${SKILL_ID}/versions`,
      ),
      expect.objectContaining({ credentials: "include" }),
    );
  });
});
