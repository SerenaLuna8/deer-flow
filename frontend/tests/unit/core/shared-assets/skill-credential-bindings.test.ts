import { describe, expect, test } from "@rstest/core";

import { projectSkillCredentialBindingsKey } from "@/core/shared-assets/query-keys";
import {
  skillCredentialBindingsInputSchema,
  skillCredentialBindingsResponseSchema,
} from "@/core/shared-assets/types";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const SKILL_ID = "33333333-3333-4333-8333-333333333333";
const SKILL_VERSION_ID = "44444444-4444-4444-8444-444444444444";
const CREDENTIAL_ID = "55555555-5555-4555-8555-555555555555";
const CREDENTIAL_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const eligibleCredential = {
  credential_id: CREDENTIAL_ID,
  credential_version_id: CREDENTIAL_VERSION_ID,
  display_name: "Weather production",
  version_number: 2,
};

const response = {
  skill_id: SKILL_ID,
  skill_version_id: SKILL_VERSION_ID,
  revision: 3,
  requirements: [
    {
      name: "WEATHER_API_KEY",
      optional: false,
      configured: true,
      credential_id: CREDENTIAL_ID,
      credential_version_id: CREDENTIAL_VERSION_ID,
      credential_display_name: "Weather production",
      credential_version_number: 2,
      eligible_credentials: [eligibleCredential],
    },
    {
      name: "OPTIONAL_REGION",
      optional: true,
      configured: false,
      credential_id: null,
      credential_version_id: null,
      credential_display_name: null,
      credential_version_number: null,
      eligible_credentials: [],
    },
  ],
  request_id: "request-bindings",
};

describe("Skill Credential binding contracts", () => {
  test("strictly accepts metadata-only bindings and rejects secret-bearing fields", () => {
    expect(skillCredentialBindingsResponseSchema.parse(response)).toEqual(
      response,
    );

    expect(
      skillCredentialBindingsResponseSchema.safeParse({
        ...response,
        requirements: [
          {
            ...response.requirements[0],
            eligible_credentials: [
              {
                ...eligibleCredential,
                secret_value: "must-never-enter-query-cache",
              },
            ],
          },
        ],
      }).success,
    ).toBe(false);
    expect(
      skillCredentialBindingsResponseSchema.safeParse({
        ...response,
        plaintext: "must-never-enter-query-cache",
      }).success,
    ).toBe(false);
    expect(
      skillCredentialBindingsResponseSchema.safeParse({
        ...response,
        requirements: [
          {
            ...response.requirements[1],
            credential_display_name: "inconsistent metadata",
          },
        ],
      }).success,
    ).toBe(false);
  });

  test("accepts a whole-set revision update and rejects duplicate or unknown fields", () => {
    const input = {
      expected_revision: 3,
      bindings: [
        {
          name: "WEATHER_API_KEY",
          credential_version_id: CREDENTIAL_VERSION_ID,
        },
      ],
    };
    expect(skillCredentialBindingsInputSchema.parse(input)).toEqual(input);
    expect(
      skillCredentialBindingsInputSchema.safeParse({
        expected_revision: 0,
        bindings: [],
      }).success,
    ).toBe(true);
    expect(
      skillCredentialBindingsInputSchema.safeParse({
        ...input,
        bindings: [input.bindings[0], input.bindings[0]],
      }).success,
    ).toBe(false);
    expect(
      skillCredentialBindingsInputSchema.safeParse({
        ...input,
        secret: "forbidden",
      }).success,
    ).toBe(false);
  });

  test("keys metadata by exact account, project, and Skill scope", () => {
    const key = projectSkillCredentialBindingsKey(
      ACCOUNT_ID,
      PROJECT_ID,
      SKILL_ID,
    );

    expect(key).toContain(ACCOUNT_ID);
    expect(key).toContain(PROJECT_ID);
    expect(key).toContain(SKILL_ID);
    expect(key.at(-1)).toBe("credential-bindings");
    expect(
      projectSkillCredentialBindingsKey(
        ACCOUNT_ID,
        "77777777-7777-4777-8777-777777777777",
        SKILL_ID,
      ),
    ).not.toEqual(key);
  });
});
