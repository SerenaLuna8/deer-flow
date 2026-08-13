import { describe, expect, test } from "@rstest/core";

import { skillVersionSchema } from "@/core/shared-assets";

const SKILL_ID = "00000000-0000-4000-8000-000000000010";
const VERSION_ID = "00000000-0000-4000-8000-000000000011";
const USER_ID = "00000000-0000-4000-8000-000000000012";
const TIMESTAMP = "2026-08-13T00:00:00Z";

function skillVersion() {
  return {
    id: VERSION_ID,
    skill_id: SKILL_ID,
    version_number: 1,
    workflow_status: "published" as const,
    description: "System Skill",
    frontmatter: { name: "system-skill" },
    compatibility: null,
    secret_requirements: [],
    scan_decision: "allow" as const,
    scan_rule_ids: [],
    scan_summary: {},
    file_views: [],
    supersedes_version_id: null,
    payload_checksum: "sha256:payload",
    revoked_at: null,
    revoked_by_user_id: null,
    revocation_reason_code: null,
    governance_status: "active" as const,
    binding_eligible: true,
    created_by_user_id: USER_ID,
    created_at: TIMESTAMP,
  };
}

describe("System Skill version revocation response", () => {
  test("accepts active and revoked governance snapshots", () => {
    expect(skillVersionSchema.parse(skillVersion())).toMatchObject({
      governance_status: "active",
      binding_eligible: true,
      revoked_at: null,
    });

    expect(
      skillVersionSchema.parse({
        ...skillVersion(),
        revoked_at: TIMESTAMP,
        revoked_by_user_id: USER_ID,
        revocation_reason_code: "security",
        governance_status: "revoked",
        binding_eligible: false,
      }),
    ).toMatchObject({
      governance_status: "revoked",
      binding_eligible: false,
      revocation_reason_code: "security",
    });
  });

  test("fails closed when the server omits governance fields", () => {
    const legacy: Partial<ReturnType<typeof skillVersion>> = skillVersion();
    delete legacy.revoked_at;
    delete legacy.revoked_by_user_id;
    delete legacy.revocation_reason_code;
    delete legacy.governance_status;
    delete legacy.binding_eligible;

    expect(skillVersionSchema.safeParse(legacy).success).toBe(false);
  });

  test("rejects an unknown revocation reason", () => {
    expect(
      skillVersionSchema.safeParse({
        ...skillVersion(),
        revoked_at: TIMESTAMP,
        revoked_by_user_id: USER_ID,
        revocation_reason_code: "unknown",
        governance_status: "revoked",
        binding_eligible: false,
      }).success,
    ).toBe(false);
  });

  test("rejects inconsistent governance and eligibility fields", () => {
    expect(
      skillVersionSchema.safeParse({
        ...skillVersion(),
        governance_status: "revoked",
        binding_eligible: true,
      }).success,
    ).toBe(false);
    expect(
      skillVersionSchema.safeParse({
        ...skillVersion(),
        binding_eligible: false,
      }).success,
    ).toBe(false);
  });
});
