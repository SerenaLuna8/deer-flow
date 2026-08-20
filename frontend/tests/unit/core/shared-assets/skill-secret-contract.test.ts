import { describe, expect, test } from "@rstest/core";

import {
  buildSkillPublishInput,
  missingRequiredSkillPublishRequirements,
  skillFrontmatterPatchInputSchema,
  skillFrontmatterParseResponseSchema,
  skillFrontmatterPatchResponseSchema,
  skillPublishRequiredBindingsBlocked,
  skillPublishPlanResponseSchema,
} from "@/core/shared-assets/skill-secret-declarations";
import {
  skillCredentialBindingsInputSchema,
  skillPublishAssetVersionInputSchema,
} from "@/core/shared-assets/types";

const SKILL_ID = "33333333-3333-4333-8333-333333333333";
const VERSION_ID = "44444444-4444-4444-8444-444444444444";
const CREDENTIAL_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const SHA = "a".repeat(64);

function publishPlan(
  requirements: Array<{
    name: string;
    optional: boolean;
    mapping_status: "missing" | "configured" | "invalid";
  }>,
) {
  const required = requirements.filter((requirement) => !requirement.optional);
  const configuredRequired = required.filter(
    (requirement) => requirement.mapping_status === "configured",
  );
  const invalid = requirements.filter(
    (requirement) => requirement.mapping_status === "invalid",
  );
  return {
    skill_id: SKILL_ID,
    skill_version_id: VERSION_ID,
    asset_version: 8,
    payload_checksum: SHA,
    binding_revision: 2,
    secrets_autonomous: false,
    ready:
      required.length === configuredRequired.length && invalid.length === 0,
    required_count: required.length,
    configured_required_count: configuredRequired.length,
    invalid_count: invalid.length,
    requirements,
    request_id: "publish-plan",
  };
}

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

  test("publish plan exposes readiness only, never selectable Credential metadata", () => {
    const plan = skillPublishPlanResponseSchema.parse(
      publishPlan([
        {
          name: "OPENAI_API_KEY",
          optional: false,
          mapping_status: "configured",
        },
      ]),
    );
    expect(plan.ready).toBe(true);
    expect(plan.requirements[0]).toEqual({
      name: "OPENAI_API_KEY",
      optional: false,
      mapping_status: "configured",
    });
    expect(() =>
      skillPublishPlanResponseSchema.parse({
        ...plan,
        requirements: [
          {
            ...plan.requirements[0],
            credential_version_id: CREDENTIAL_VERSION_ID,
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
      skillPublishPlanResponseSchema.parse(
        publishPlan([
          { name: historicalName, optional: false, mapping_status: "missing" },
        ]),
      ).requirements[0]?.name,
    ).toBe(historicalName);
  });

  test("publish body always pins payload and binding revision but rejects selections", () => {
    const plan = skillPublishPlanResponseSchema.parse(publishPlan([]));
    const input = skillPublishAssetVersionInputSchema.parse(
      buildSkillPublishInput({ plan }),
    );
    expect(input).toEqual({
      expected_asset_version: 8,
      expected_payload_checksum: SHA,
      expected_binding_revision: 2,
      acknowledge_stale_base: false,
    });
    expect(input).not.toHaveProperty("credential_bindings");
    expect(() =>
      skillPublishAssetVersionInputSchema.parse({
        ...input,
        credential_bindings: [],
      }),
    ).toThrow();
  });

  test("binding body maps a target name to a specific Credential env field", () => {
    expect(
      skillCredentialBindingsInputSchema.parse({
        expected_revision: 2,
        bindings: [
          {
            name: "OPENAI_API_KEY",
            credential_version_id: CREDENTIAL_VERSION_ID,
            source_env_field_name: "PROVIDER_TOKEN",
          },
        ],
      }),
    ).toEqual({
      expected_revision: 2,
      bindings: [
        {
          name: "OPENAI_API_KEY",
          credential_version_id: CREDENTIAL_VERSION_ID,
          source_env_field_name: "PROVIDER_TOKEN",
        },
      ],
    });
  });

  test("rejects a publish summary that disagrees with requirement status", () => {
    expect(() =>
      skillPublishPlanResponseSchema.parse({
        ...publishPlan([
          { name: "API_KEY", optional: false, mapping_status: "missing" },
        ]),
        ready: true,
      }),
    ).toThrow();
  });

  test("blocks every public Draft until the read-only preflight is ready", () => {
    const blocked = skillPublishPlanResponseSchema.parse(
      publishPlan([
        { name: "API_KEY", optional: false, mapping_status: "missing" },
        { name: "OPTIONAL", optional: true, mapping_status: "invalid" },
      ]),
    );
    expect(missingRequiredSkillPublishRequirements(blocked)).toMatchObject([
      { name: "API_KEY" },
    ]);
    expect(skillPublishRequiredBindingsBlocked({ plan: blocked })).toBe(true);

    const ready = skillPublishPlanResponseSchema.parse(
      publishPlan([
        { name: "API_KEY", optional: false, mapping_status: "configured" },
      ]),
    );
    expect(skillPublishRequiredBindingsBlocked({ plan: ready })).toBe(false);
  });
});
