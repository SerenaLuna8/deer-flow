import { describe, expect, test } from "@rstest/core";

import { userSchema } from "@/core/auth/types";
import { PROJECT_PRIVATE_WORKSPACE } from "@/core/projects/features";

describe("project account prerequisites", () => {
  test("keeps platform roles separate from project admin membership", () => {
    const base = { id: "u1", email: "a@example.com" };
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

  test("hard-disables private project work for M1", () => {
    expect(PROJECT_PRIVATE_WORKSPACE).toBe(false);
  });
});
