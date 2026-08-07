import { describe, expect, test } from "@rstest/core";

import { credentialReplacementResponseSchema } from "@/core/shared-assets";

const version = {
  id: "11111111-1111-4111-8111-111111111111",
  credential_id: "22222222-2222-4222-8222-222222222222",
  version_number: 2,
  status: "active",
  payload_schema_version: 1,
  payload_schema: { env: ["DEEPSEEK_API_KEY"] },
  supersedes_version_id: "33333333-3333-4333-8333-333333333333",
  created_by_user_id: "44444444-4444-4444-8444-444444444444",
  created_at: "2026-08-07T00:00:00.000Z",
};

function response(pendingMigration: unknown) {
  return {
    data: version,
    pending_migration: pendingMigration,
    request_id: "req-1",
  };
}

describe("credential replacement contract", () => {
  test("keeps the server-computed pending report, including its model share", () => {
    const parsed = credentialReplacementResponseSchema.parse(
      response({ total: 3, system_model_count: 1 }),
    );

    expect(parsed.pending_migration).toEqual({
      total: 3,
      system_model_count: 1,
    });
  });

  test("accepts an explicitly unavailable report without inventing a zero", () => {
    const parsed = credentialReplacementResponseSchema.parse(response(null));

    expect(parsed.pending_migration).toBeNull();
  });

  test("rejects a replacement that omits the pending report", () => {
    expect(() =>
      credentialReplacementResponseSchema.parse({
        data: version,
        request_id: "req-1",
      }),
    ).toThrow();
  });

  test("rejects a pending report the browser could partially fabricate", () => {
    expect(() =>
      credentialReplacementResponseSchema.parse(response({ total: 2 })),
    ).toThrow();
    expect(() =>
      credentialReplacementResponseSchema.parse(
        response({ total: -1, system_model_count: 0 }),
      ),
    ).toThrow();
    expect(() =>
      credentialReplacementResponseSchema.parse(
        response({ total: 2, system_model_count: 1, migrated_count: 2 }),
      ),
    ).toThrow();
  });
});
