import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  getProjectSkillPublishPlan,
  parseProjectSkillFrontmatter,
  patchProjectSkillFrontmatter,
  publishProjectAssetVersion,
  type SharedAssetApiError,
} from "@/core/shared-assets";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const SKILL_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_ID = "33333333-3333-4333-8333-333333333333";
const CREDENTIAL_ID = "44444444-4444-4444-8444-444444444444";
const CREDENTIAL_VERSION_ID = "55555555-5555-4555-8555-555555555555";
const USER_ID = "66666666-6666-4666-8666-666666666666";
const SHA = "a".repeat(64);
const CONTENT = "---\nname: example\n---\n";

afterEach(() => {
  rs.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200) {
  return Response.json(body, { status });
}

describe("Skill secret API", () => {
  test("uses the no-YAML client parse and patch contracts", async () => {
    const parseResponse = {
      source_sha256: SHA,
      valid: true,
      patchable: true,
      projection: {
        required_secrets: [],
        secrets_autonomous: false,
        secrets_autonomous_explicit: false,
        shorthand_count: 0,
      },
      diagnostics: [],
      request_id: "parse-request",
    };
    const patchedContent =
      '---\nname: example\nrequired-secrets:\n  - name: "API_KEY"\n    optional: false\nsecrets-autonomous: false\n---\n';
    const patchResponse = {
      source_sha256: SHA,
      result_sha256: "b".repeat(64),
      content: patchedContent,
      changed: true,
      changed_fields: ["required-secrets"],
      projection: {
        required_secrets: [{ name: "API_KEY", optional: false }],
        secrets_autonomous: false,
        secrets_autonomous_explicit: true,
        shorthand_count: 0,
      },
      diagnostics: [],
      request_id: "patch-request",
    };
    const fetchMock = rs
      .fn()
      .mockResolvedValueOnce(jsonResponse(parseResponse))
      .mockResolvedValueOnce(jsonResponse(patchResponse));
    rs.stubGlobal("fetch", fetchMock);

    await expect(
      parseProjectSkillFrontmatter(PROJECT_ID, {
        content: CONTENT,
        source_sha256: SHA,
      }),
    ).resolves.toEqual(parseResponse);
    await expect(
      patchProjectSkillFrontmatter(PROJECT_ID, {
        content: CONTENT,
        source_sha256: SHA,
        required_secrets: [{ name: "API_KEY", optional: false }],
        secrets_autonomous: false,
      }),
    ).resolves.toEqual(patchResponse);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining(
        `/api/projects/${PROJECT_ID}/skills/frontmatter/parse`,
      ),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ content: CONTENT, source_sha256: SHA }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining(
        `/api/projects/${PROJECT_ID}/skills/frontmatter/patch`,
      ),
      expect.objectContaining({ method: "POST" }),
    );
  });

  test("loads a version-bound publish plan and rejects a mismatched identity", async () => {
    const plan = {
      skill_id: SKILL_ID,
      skill_version_id: VERSION_ID,
      asset_version: 4,
      payload_checksum: SHA,
      binding_revision: 1,
      secrets_autonomous: false,
      requirements: [
        {
          name: "API_KEY",
          optional: false,
          suggested_credential_version_id: CREDENTIAL_VERSION_ID,
          eligible_credentials: [
            {
              credential_id: CREDENTIAL_ID,
              credential_version_id: CREDENTIAL_VERSION_ID,
              display_name: "Production key",
              version_number: 2,
            },
          ],
        },
      ],
      request_id: "plan-request",
    };
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => jsonResponse(plan)),
    );
    await expect(
      getProjectSkillPublishPlan(PROJECT_ID, SKILL_ID, VERSION_ID),
    ).resolves.toEqual(plan);

    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        jsonResponse({
          ...plan,
          skill_id: "77777777-7777-4777-8777-777777777777",
        }),
      ),
    );
    await expect(
      getProjectSkillPublishPlan(PROJECT_ID, SKILL_ID, VERSION_ID),
    ).rejects.toMatchObject({
      code: "ASSET_RESPONSE_INVALID",
    } satisfies Partial<SharedAssetApiError>);
  });

  test("publishes the target version and exact Credential IDs atomically", async () => {
    const response = {
      data: {
        id: VERSION_ID,
        skill_id: SKILL_ID,
        version_number: 2,
        workflow_status: "published",
        description: "Example",
        frontmatter: { name: "example" },
        compatibility: null,
        secret_requirements: [{ name: "API_KEY", optional: false }],
        scan_decision: "allow",
        scan_rule_ids: [],
        scan_summary: {},
        file_views: [
          {
            path: "SKILL.md",
            media_type: "text/markdown",
            size_bytes: 25,
            sha256: SHA,
          },
        ],
        supersedes_version_id: null,
        payload_checksum: SHA,
        revoked_at: null,
        revoked_by_user_id: null,
        revocation_reason_code: null,
        governance_status: "active",
        binding_eligible: true,
        created_by_user_id: USER_ID,
        created_at: "2026-08-19T09:00:00Z",
      },
      request_id: "publish-request",
    };
    const fetchMock = rs.fn(async () => jsonResponse(response));
    rs.stubGlobal("fetch", fetchMock);
    const input = {
      expected_asset_version: 4,
      expected_payload_checksum: SHA,
      expected_binding_revision: 1,
      credential_bindings: [
        {
          name: "API_KEY",
          credential_version_id: CREDENTIAL_VERSION_ID,
        },
      ],
    };

    await expect(
      publishProjectAssetVersion(
        PROJECT_ID,
        "skills",
        SKILL_ID,
        VERSION_ID,
        input,
      ),
    ).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(
        `/api/projects/${PROJECT_ID}/skills/${SKILL_ID}/versions/${VERSION_ID}/publish`,
      ),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
  });

  test("preserves the dedicated invalid-declaration code with safe diagnostics", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        jsonResponse(
          {
            detail: {
              code: "SKILL_SECRET_DECLARATION_INVALID",
              message: "Skill secret declaration is invalid",
              request_id: "error-request",
              diagnostics: [
                {
                  code: "invalid_env_name",
                  severity: "error",
                  field_path: ["required-secrets", 0, "name"],
                  line: 4,
                  column: 11,
                  public_message: "Environment variable name is invalid",
                },
              ],
            },
          },
          422,
        ),
      ),
    );

    await expect(
      patchProjectSkillFrontmatter(PROJECT_ID, {
        content: CONTENT,
        source_sha256: SHA,
        required_secrets: [],
        secrets_autonomous: false,
      }),
    ).rejects.toMatchObject({
      status: 422,
      code: "SKILL_SECRET_DECLARATION_INVALID",
      diagnostics: [
        {
          code: "invalid_env_name",
          severity: "error",
          field_path: ["required-secrets", 0, "name"],
          line: 4,
          column: 11,
          public_message: "Environment variable name is invalid",
        },
      ],
    } satisfies Partial<SharedAssetApiError>);
  });
});
