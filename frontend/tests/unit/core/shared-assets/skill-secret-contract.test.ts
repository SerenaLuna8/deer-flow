import { describe, expect, test } from "@rstest/core";

import {
  buildSkillActivationInput,
  missingRequiredSkillActivationRequirements,
  skillActivationBlocked,
  skillActivationReadinessResponseSchema,
} from "@/core/shared-assets/skill-secret-declarations";
import {
  skillActivationInputSchema,
  skillSecretReplaceInputSchema,
  skillSecretSetResponseSchema,
} from "@/core/shared-assets/types";

const SKILL_ID = "33333333-3333-4333-8333-333333333333";
const VERSION_ID = "44444444-4444-4444-8444-444444444444";
const SHA = "a".repeat(64);

function readiness(configured: boolean) {
  return {
    skill_id: SKILL_ID,
    skill_version_id: VERSION_ID,
    revision: 8,
    payload_checksum: SHA,
    secret_revision: 2,
    secrets_autonomous: false,
    ready: configured,
    required_count: 1,
    configured_required_count: configured ? 1 : 0,
    requirements: [{ name: "API_KEY", optional: false, configured }],
    request_id: "activation-readiness",
  };
}

describe("Skill domain secret contracts", () => {
  test("status response is write-only and rejects plaintext fields", () => {
    const status = skillSecretSetResponseSchema.parse({
      skill_id: SKILL_ID,
      skill_version_id: VERSION_ID,
      revision: 2,
      readiness: "ready",
      requirements: [
        { name: "API_KEY", optional: false, configured: true, revision: 2 },
      ],
      request_id: "status",
    });
    expect(status.requirements[0]?.configured).toBe(true);
    expect(() =>
      skillSecretSetResponseSchema.parse({
        ...status,
        requirements: [
          { ...status.requirements[0], value: "must-never-return" },
        ],
      }),
    ).toThrow();
  });

  test("replace body accepts non-empty exact secret names", () => {
    expect(
      skillSecretReplaceInputSchema.parse({
        secrets: { API_KEY: "temporary-value" },
      }),
    ).toEqual({ secrets: { API_KEY: "temporary-value" } });
    expect(() => skillSecretReplaceInputSchema.parse({ secrets: {} })).toThrow();
  });

  test("activation pins payload and secret revision without secret values", () => {
    const plan = skillActivationReadinessResponseSchema.parse(readiness(true));
    expect(
      skillActivationInputSchema.parse(
        buildSkillActivationInput({ readiness: plan }),
      ),
    ).toEqual({
      expected_revision: 8,
      expected_payload_checksum: SHA,
      expected_secret_revision: 2,
    });
  });

  test("required missing status blocks activation", () => {
    const blocked = skillActivationReadinessResponseSchema.parse(readiness(false));
    expect(missingRequiredSkillActivationRequirements(blocked)).toEqual([
      { name: "API_KEY", optional: false, configured: false },
    ]);
    expect(skillActivationBlocked({ readiness: blocked })).toBe(true);
  });
});
