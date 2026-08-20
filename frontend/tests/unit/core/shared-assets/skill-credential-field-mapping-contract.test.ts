import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  getProjectSkillCredentialBindings,
  skillCredentialBindingsInputSchema,
  skillCredentialBindingsResponseSchema,
  skillPublishAssetVersionInputSchema,
  skillPublishPlanResponseSchema,
  updateProjectSkillCredentialBindings,
} from "@/core/shared-assets";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const SKILL_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_ID = "33333333-3333-4333-8333-333333333333";
const CREDENTIAL_ID = "44444444-4444-4444-8444-444444444444";
const CREDENTIAL_VERSION_ID = "55555555-5555-4555-8555-555555555555";
const SHA = "a".repeat(64);

const mappedResponse = {
  skill_id: SKILL_ID,
  skill_version_id: VERSION_ID,
  revision: 3,
  requirements: [
    {
      name: "TEXT_ROUTE_DB_NAME",
      optional: false,
      configured: true,
      mapping_status: "configured",
      credential_id: CREDENTIAL_ID,
      credential_version_id: CREDENTIAL_VERSION_ID,
      credential_display_name: "Text route database",
      credential_version_number: 2,
      source_env_field_name: "DB_DATABASE",
      eligible_credentials: [
        {
          credential_id: CREDENTIAL_ID,
          credential_version_id: CREDENTIAL_VERSION_ID,
          display_name: "Text route database",
          version_number: 2,
          env_fields: ["DB_HOST", "DB_PORT", "DB_DATABASE"],
        },
      ],
    },
  ],
  request_id: "binding-request",
};

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("Skill Credential field-mapping contract", () => {
  test("accepts only safe env-field metadata and supports an aliased source", () => {
    expect(skillCredentialBindingsResponseSchema.parse(mappedResponse)).toEqual(
      mappedResponse,
    );
    expect(() =>
      skillCredentialBindingsResponseSchema.parse({
        ...mappedResponse,
        requirements: [
          {
            ...mappedResponse.requirements[0],
            eligible_credentials: [
              {
                ...mappedResponse.requirements[0]?.eligible_credentials[0],
                env_values: { DB_DATABASE: "must-not-reach-browser" },
              },
            ],
          },
        ],
      }),
    ).toThrow();
  });

  test("accepts redacted status and repairable partial metadata without values", () => {
    expect(
      skillCredentialBindingsResponseSchema.parse({
        ...mappedResponse,
        requirements: [
          {
            name: "TEXT_ROUTE_DB_NAME",
            optional: false,
            configured: true,
            mapping_status: "configured",
            credential_id: null,
            credential_version_id: null,
            credential_display_name: null,
            credential_version_number: null,
            source_env_field_name: null,
            eligible_credentials: [],
          },
        ],
      }),
    ).toBeTruthy();
    expect(
      skillCredentialBindingsResponseSchema.parse({
        ...mappedResponse,
        requirements: [
          {
            name: "TEXT_ROUTE_DB_NAME",
            optional: false,
            configured: true,
            mapping_status: "invalid",
            credential_id: CREDENTIAL_ID,
            credential_version_id: CREDENTIAL_VERSION_ID,
            credential_display_name: null,
            credential_version_number: null,
            source_env_field_name: "DB_DATABASE",
            eligible_credentials: [],
          },
        ],
      }),
    ).toBeTruthy();
  });

  test("writes target-to-source mappings with revision CAS only", () => {
    expect(
      skillCredentialBindingsInputSchema.parse({
        expected_revision: 3,
        bindings: [
          {
            name: "TEXT_ROUTE_DB_NAME",
            credential_version_id: CREDENTIAL_VERSION_ID,
            source_env_field_name: "DB_DATABASE",
          },
        ],
      }),
    ).toEqual({
      expected_revision: 3,
      bindings: [
        {
          name: "TEXT_ROUTE_DB_NAME",
          credential_version_id: CREDENTIAL_VERSION_ID,
          source_env_field_name: "DB_DATABASE",
        },
      ],
    });
  });

  test("uses the exact Skill version in the binding URL", async () => {
    const fetchMock = rs.fn(async () => Response.json(mappedResponse));
    rs.stubGlobal("fetch", fetchMock);

    await expect(
      getProjectSkillCredentialBindings(PROJECT_ID, SKILL_ID, VERSION_ID),
    ).resolves.toEqual(mappedResponse);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining(
        `/api/projects/${PROJECT_ID}/skills/${SKILL_ID}/versions/${VERSION_ID}/credential-bindings`,
      ),
      expect.anything(),
    );

    const input = {
      expected_revision: 3,
      bindings: [
        {
          name: "TEXT_ROUTE_DB_NAME",
          credential_version_id: CREDENTIAL_VERSION_ID,
          source_env_field_name: "DB_DATABASE",
        },
      ],
    };
    await expect(
      updateProjectSkillCredentialBindings(
        PROJECT_ID,
        SKILL_ID,
        VERSION_ID,
        input,
      ),
    ).resolves.toEqual(mappedResponse);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining(
        `/api/projects/${PROJECT_ID}/skills/${SKILL_ID}/versions/${VERSION_ID}/credential-bindings`,
      ),
      expect.objectContaining({ method: "PUT", body: JSON.stringify(input) }),
    );
  });

  test("publish plan is read-only preflight and publish rejects inline binding choices", () => {
    const plan = {
      skill_id: SKILL_ID,
      skill_version_id: VERSION_ID,
      asset_version: 8,
      payload_checksum: SHA,
      binding_revision: 3,
      secrets_autonomous: true,
      ready: true,
      required_count: 1,
      configured_required_count: 1,
      invalid_count: 0,
      requirements: [
        {
          name: "TEXT_ROUTE_DB_NAME",
          optional: false,
          mapping_status: "configured",
        },
      ],
      request_id: "publish-plan",
    };
    expect(skillPublishPlanResponseSchema.parse(plan)).toEqual(plan);
    expect(() =>
      skillPublishAssetVersionInputSchema.parse({
        expected_asset_version: 8,
        expected_payload_checksum: SHA,
        expected_binding_revision: 3,
        credential_bindings: [
          {
            name: "TEXT_ROUTE_DB_NAME",
            credential_version_id: CREDENTIAL_VERSION_ID,
            source_env_field_name: "DB_DATABASE",
          },
        ],
      }),
    ).toThrow();
  });
});
