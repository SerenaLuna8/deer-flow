import { describe, expect, test } from "@rstest/core";

import { parseAuthError } from "@/core/auth/types";

describe("auth error contract", () => {
  test("preserves the structured registration-disabled error", () => {
    expect(
      parseAuthError({
        detail: {
          code: "registration_disabled",
          message: "Self-registration is disabled on this deployment",
        },
      }),
    ).toEqual({
      code: "registration_disabled",
      message: "Self-registration is disabled on this deployment",
    });
  });
});
