import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  canCreateRegularAccount,
  fetchSetupStatus,
  isSystemAlreadyInitializedError,
  setupStatusFetchInit,
  setupStatusSchema,
} from "@/core/auth/setup";

describe("auth setup helpers", () => {
  afterEach(() => {
    rs.unstubAllGlobals();
  });

  test("setup-status requests bypass browser caches", () => {
    expect(setupStatusFetchInit).toMatchObject({
      cache: "no-store",
      credentials: "include",
    });
  });

  test("fetchSetupStatus uses the shared no-store request options", async () => {
    const fetchMock = rs.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            needs_setup: true,
            registration_enabled: false,
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );
    rs.stubGlobal("fetch", fetchMock);

    await expect(fetchSetupStatus()).resolves.toEqual({
      needs_setup: true,
      registration_enabled: false,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/setup-status",
      expect.objectContaining({
        ...setupStatusFetchInit,
        signal: expect.any(AbortSignal),
      }),
    );
  });

  test("setup status is strict and fails closed on missing or private fields", () => {
    expect(
      setupStatusSchema.safeParse({
        needs_setup: false,
        registration_enabled: true,
      }).success,
    ).toBe(true);
    expect(
      setupStatusSchema.safeParse({
        needs_setup: false,
      }).success,
    ).toBe(false);
    expect(
      setupStatusSchema.safeParse({
        needs_setup: false,
        registration_enabled: true,
        internal_registration_policy: "open",
      }).success,
    ).toBe(false);
  });

  test("regular sign-up requires known setup state and an enabled registration gate", () => {
    expect(canCreateRegularAccount({ checked: false, status: null })).toBe(
      false,
    );
    expect(
      canCreateRegularAccount({
        checked: true,
        status: { needs_setup: true, registration_enabled: true },
      }),
    ).toBe(false);
    expect(
      canCreateRegularAccount({
        checked: true,
        status: { needs_setup: false, registration_enabled: true },
      }),
    ).toBe(true);
    expect(
      canCreateRegularAccount({
        checked: true,
        status: { needs_setup: false, registration_enabled: false },
      }),
    ).toBe(false);
    expect(canCreateRegularAccount({ checked: true, status: null })).toBe(
      false,
    );
  });

  test("detects already-initialized setup conflicts", () => {
    expect(
      isSystemAlreadyInitializedError({
        detail: {
          code: "system_already_initialized",
          message: "System already initialized",
        },
      }),
    ).toBe(true);

    expect(
      isSystemAlreadyInitializedError({
        detail: {
          code: "invalid_credentials",
          message: "Wrong password",
        },
      }),
    ).toBe(false);
  });
});
