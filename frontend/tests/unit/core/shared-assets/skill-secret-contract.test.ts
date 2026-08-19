import { describe, expect, test } from "@rstest/core";

import {
  buildSkillPublishInput,
  initialSkillPublishSelections,
  mergeSkillPublishSelections,
  missingRequiredSkillPublishRequirements,
  skillFrontmatterPatchInputSchema,
  skillFrontmatterParseResponseSchema,
  skillFrontmatterPatchResponseSchema,
  skillPublishRequiredBindingsBlocked,
  skillPublishPlanResponseSchema,
} from "@/core/shared-assets/skill-secret-declarations";
import { skillPublishAssetVersionInputSchema } from "@/core/shared-assets/types";

const SKILL_ID = "33333333-3333-4333-8333-333333333333";
const VERSION_ID = "44444444-4444-4444-8444-444444444444";
const CREDENTIAL_ID = "55555555-5555-4555-8555-555555555555";
const CREDENTIAL_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const SHA = "a".repeat(64);

describe("Skill secret declaration contracts", () => {
  test("accepts a safe invalid parse projection without returning source text", () => {
    const result = skillFrontmatterParseResponseSchema.parse({
      source_sha256: SHA,
      valid: false,
      patchable: false,
      projection: null,
      diagnostics: [
        {
          code: "invalid_yaml",
          severity: "error",
          field_path: ["required-secrets", 0, "name"],
          line: 4,
          column: 7,
          public_message: "Skill frontmatter is invalid",
        },
      ],
      request_id: "req-1",
    });

    expect(result.valid).toBe(false);
    expect(result).not.toHaveProperty("content");
  });

  test("rejects unknown fields in parse and patch responses", () => {
    expect(() =>
      skillFrontmatterParseResponseSchema.parse({
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
        request_id: "req-1",
        plaintext: "must never reach the UI",
      }),
    ).toThrow();

    expect(() =>
      skillFrontmatterPatchResponseSchema.parse({
        source_sha256: SHA,
        result_sha256: SHA,
        content: "---\nname: safe\n---\n",
        changed: false,
        changed_fields: [],
        projection: {
          required_secrets: [],
          secrets_autonomous: false,
          secrets_autonomous_explicit: false,
          shorthand_count: 0,
        },
        diagnostics: [],
        request_id: "req-1",
        secret_value: "must never reach the UI",
      }),
    ).toThrow();
  });

  test("accepts only server-supplied eligible Credential metadata", () => {
    const plan = skillPublishPlanResponseSchema.parse({
      skill_id: SKILL_ID,
      skill_version_id: VERSION_ID,
      asset_version: 8,
      payload_checksum: SHA,
      binding_revision: 0,
      secrets_autonomous: true,
      requirements: [
        {
          name: "OPENAI_API_KEY",
          optional: false,
          suggested_credential_version_id: CREDENTIAL_VERSION_ID,
          eligible_credentials: [
            {
              credential_id: CREDENTIAL_ID,
              credential_version_id: CREDENTIAL_VERSION_ID,
              display_name: "OpenAI production",
              version_number: 3,
            },
          ],
        },
      ],
      request_id: "req-2",
    });

    expect(plan.requirements[0]?.eligible_credentials).toHaveLength(1);
    expect(() =>
      skillPublishPlanResponseSchema.parse({
        ...plan,
        requirements: [
          {
            ...plan.requirements[0],
            eligible_credentials: [
              {
                ...plan.requirements[0]?.eligible_credentials[0],
                plaintext: "leak",
              },
            ],
          },
        ],
      }),
    ).toThrow();
  });

  test("keeps canonical declarations readable beyond the binding write limit", () => {
    const historicalName = `A${"B".repeat(300)}`;
    expect(
      skillFrontmatterPatchInputSchema.parse({
        content: "---\nname: example\n---\n",
        source_sha256: SHA,
        required_secrets: [{ name: historicalName, optional: false }],
        secrets_autonomous: false,
      }).required_secrets[0]?.name,
    ).toBe(historicalName);
    expect(
      skillPublishPlanResponseSchema.parse({
        skill_id: SKILL_ID,
        skill_version_id: VERSION_ID,
        asset_version: 8,
        payload_checksum: SHA,
        binding_revision: 0,
        secrets_autonomous: false,
        requirements: [
          {
            name: historicalName,
            optional: false,
            suggested_credential_version_id: null,
            eligible_credentials: [],
          },
        ],
        request_id: "req-long-binding-name",
      }).requirements[0]?.name,
    ).toBe(historicalName);
    expect(() =>
      skillPublishAssetVersionInputSchema.parse({
        expected_asset_version: 8,
        expected_payload_checksum: SHA,
        expected_binding_revision: 0,
        acknowledge_stale_base: false,
        credential_bindings: [
          {
            name: historicalName,
            credential_version_id: CREDENTIAL_VERSION_ID,
          },
        ],
      }),
    ).toThrow();
  });

  test("builds the atomic publish body without any secret value field", () => {
    const input = skillPublishAssetVersionInputSchema.parse({
      expected_asset_version: 8,
      expected_payload_checksum: SHA,
      expected_binding_revision: 0,
      acknowledge_stale_base: false,
      credential_bindings: [
        {
          name: "OPENAI_API_KEY",
          credential_version_id: CREDENTIAL_VERSION_ID,
        },
      ],
    });

    expect(input.credential_bindings).toEqual([
      {
        name: "OPENAI_API_KEY",
        credential_version_id: CREDENTIAL_VERSION_ID,
      },
    ]);
    expect(JSON.stringify(input)).not.toMatch(
      /plaintext|secret_value|credential_payload/u,
    );
  });

  test("preserves only still-eligible selections across a stale-plan refresh", () => {
    const plan = skillPublishPlanResponseSchema.parse({
      skill_id: SKILL_ID,
      skill_version_id: VERSION_ID,
      asset_version: 8,
      payload_checksum: SHA,
      binding_revision: 0,
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
              display_name: "Primary",
              version_number: 1,
            },
          ],
        },
        {
          name: "OPTIONAL_TOKEN",
          optional: true,
          suggested_credential_version_id: null,
          eligible_credentials: [],
        },
      ],
      request_id: "req-before",
    });
    expect(initialSkillPublishSelections(plan)).toEqual({
      API_KEY: CREDENTIAL_VERSION_ID,
    });

    const refreshed = {
      ...plan,
      asset_version: 9,
      request_id: "req-after",
      requirements: plan.requirements.map((requirement) =>
        requirement.name === "API_KEY"
          ? { ...requirement, suggested_credential_version_id: null }
          : requirement,
      ),
    };
    expect(
      mergeSkillPublishSelections(
        {
          API_KEY: CREDENTIAL_VERSION_ID,
          OPTIONAL_TOKEN: "77777777-7777-4777-8777-777777777777",
        },
        refreshed,
      ),
    ).toEqual({ API_KEY: CREDENTIAL_VERSION_ID });
    expect(
      missingRequiredSkillPublishRequirements(refreshed, {}),
    ).toMatchObject([{ name: "API_KEY" }]);
  });

  test("preserves an intentional unbound selection across publish-plan refetch", () => {
    const optionalVersionId = "77777777-7777-4777-8777-777777777777";
    const plan = skillPublishPlanResponseSchema.parse({
      skill_id: SKILL_ID,
      skill_version_id: VERSION_ID,
      asset_version: 8,
      payload_checksum: SHA,
      binding_revision: 2,
      secrets_autonomous: false,
      requirements: [
        {
          name: "OPTIONAL_TOKEN",
          optional: true,
          suggested_credential_version_id: optionalVersionId,
          eligible_credentials: [
            {
              credential_id: CREDENTIAL_ID,
              credential_version_id: optionalVersionId,
              display_name: "Optional",
              version_number: 1,
            },
          ],
        },
      ],
      request_id: "req-refetched",
    });

    expect(
      mergeSkillPublishSelections({}, plan, new Set(["OPTIONAL_TOKEN"])),
    ).toEqual({});
  });

  test("omits Credential replacement intent without approve capability", () => {
    const plan = skillPublishPlanResponseSchema.parse({
      skill_id: SKILL_ID,
      skill_version_id: VERSION_ID,
      asset_version: 8,
      payload_checksum: SHA,
      binding_revision: 2,
      secrets_autonomous: false,
      requirements: [],
      request_id: "req-no-secrets",
    });
    expect(
      buildSkillPublishInput({
        plan,
        selections: {},
        includeCredentialBindings: false,
      }),
    ).not.toHaveProperty("credential_bindings");
  });

  test("blocks only active required bindings according to approval authority", () => {
    const plan = skillPublishPlanResponseSchema.parse({
      skill_id: SKILL_ID,
      skill_version_id: VERSION_ID,
      asset_version: 8,
      payload_checksum: SHA,
      binding_revision: 0,
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
              display_name: "Prior published binding",
              version_number: 1,
            },
          ],
        },
      ],
      request_id: "req-permission",
    });

    expect(
      skillPublishRequiredBindingsBlocked({
        plan,
        selections: { API_KEY: CREDENTIAL_VERSION_ID },
        skillActive: true,
        canApproveCredentials: false,
      }),
    ).toBe(true);
    expect(
      skillPublishRequiredBindingsBlocked({
        plan,
        selections: { API_KEY: CREDENTIAL_VERSION_ID },
        skillActive: true,
        canApproveCredentials: true,
      }),
    ).toBe(false);
    expect(
      skillPublishRequiredBindingsBlocked({
        plan,
        selections: {},
        skillActive: true,
        canApproveCredentials: true,
      }),
    ).toBe(true);
    expect(
      skillPublishRequiredBindingsBlocked({
        plan,
        selections: {},
        skillActive: false,
        canApproveCredentials: true,
      }),
    ).toBe(false);
    expect(
      skillPublishRequiredBindingsBlocked({
        plan,
        selections: {},
        skillActive: false,
        canApproveCredentials: false,
      }),
    ).toBe(false);
  });
});
