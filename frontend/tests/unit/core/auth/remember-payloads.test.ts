import { describe, expect, test } from "@rstest/core";

import {
  buildLocalLoginBody,
  buildOidcLoginUrl,
  buildRememberingCredentialPayload,
  buildSetupPasswordChangePayload,
} from "@/core/auth/remember-payloads";

describe("remember-me auth payloads", () => {
  test("local login sends an explicit form boolean without losing credentials", () => {
    const params = new URLSearchParams(
      buildLocalLoginBody({
        email: "user+auth@example.com",
        password: "s3cret&value",
        rememberMe: false,
      }),
    );
    expect(Object.fromEntries(params)).toEqual({
      username: "user+auth@example.com",
      password: "s3cret&value",
      remember_me: "false",
    });
  });

  test("register and initialize send the backend boolean contract", () => {
    expect(
      buildRememberingCredentialPayload({
        email: "user@example.com",
        password: "s3cret-value",
        rememberMe: true,
      }),
    ).toEqual({
      email: "user@example.com",
      password: "s3cret-value",
      remember_me: true,
    });
  });

  test("OIDC carries next and remember preference in signed-state inputs", () => {
    expect(
      buildOidcLoginUrl({
        providerId: "company/oidc",
        next: "/projects/example?tab=members#active",
        rememberMe: false,
      }),
    ).toBe(
      "/api/v1/auth/oauth/company%2Foidc?next=%2Fprojects%2Fexample%3Ftab%3Dmembers%23active&remember_me=false",
    );
  });

  test("forced setup password change sends the selected preference", () => {
    expect(
      buildSetupPasswordChangePayload({
        currentPassword: "temporary",
        email: "admin@example.com",
        newPassword: "new-secret",
        rememberMe: true,
      }),
    ).toEqual({
      current_password: "temporary",
      new_password: "new-secret",
      new_email: "admin@example.com",
      remember_me: true,
    });
  });
});
