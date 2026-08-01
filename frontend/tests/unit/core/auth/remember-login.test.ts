import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  loadRememberLoginPreference,
  saveRememberLoginPreference,
} from "@/core/auth/remember-login";

function makeStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: rs.fn((key: string) => values.get(key) ?? null),
    removeItem: rs.fn((key: string) => {
      values.delete(key);
    }),
    setItem: rs.fn((key: string, value: string) => {
      values.set(key, value);
    }),
    values,
  };
}

describe("remember login preference", () => {
  afterEach(() => {
    rs.unstubAllGlobals();
  });

  test("persists only the preference and email, never a password", () => {
    const storage = makeStorage();
    rs.stubGlobal("localStorage", storage);

    saveRememberLoginPreference({
      email: "Admin@Example.com",
      rememberMe: true,
    });

    expect(loadRememberLoginPreference()).toEqual({
      email: "Admin@Example.com",
      rememberMe: true,
    });
    expect([...storage.values.values()]).not.toContain("password");
  });

  test("forgetting the session removes the remembered email", () => {
    const storage = makeStorage({
      "deerflow.auth.remember_login": "1",
      "deerflow.auth.remembered_email": "admin@example.com",
    });
    rs.stubGlobal("localStorage", storage);

    saveRememberLoginPreference({
      email: "admin@example.com",
      rememberMe: false,
    });

    expect(loadRememberLoginPreference()).toEqual({
      email: "",
      rememberMe: false,
    });
  });
});
