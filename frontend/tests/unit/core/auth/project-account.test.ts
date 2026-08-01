import { describe, expect, test } from "@rstest/core";

import { userSchema } from "@/core/auth/types";

describe("project account prerequisites", () => {
  test("keeps platform roles separate from project admin membership", () => {
    const base = {
      id: "10000000-0000-4000-8000-000000000001",
      email: "a@example.com",
      needs_setup: false,
      oauth_provider: null,
    };
    expect(
      userSchema.safeParse({ ...base, system_role: "system_admin" }).success,
    ).toBe(true);
    expect(userSchema.safeParse({ ...base, system_role: "user" }).success).toBe(
      true,
    );
    expect(
      userSchema.safeParse({ ...base, system_role: "admin" }).success,
    ).toBe(false);
  });

  test("rejects unknown authority fields in user responses", () => {
    expect(
      userSchema.safeParse({
        id: "10000000-0000-4000-8000-000000000001",
        email: "a@example.com",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
        project_role: "admin",
      }).success,
    ).toBe(false);
  });

  test("rejects non-UUID account identifiers", () => {
    expect(
      userSchema.safeParse({
        id: "user-1",
        email: "a@example.com",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      }).success,
    ).toBe(false);
  });
});
