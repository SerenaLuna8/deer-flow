import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  loadRememberLoginPreference,
  saveRememberLoginPreference,
} from "@/core/auth/remember-login";

function installStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  rs.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  return values;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("remember-login brand key migration", () => {
  test("migrates only the legacy preference and email to ActWeave keys", () => {
    const values = installStorage({
      "deerflow.auth.remember_login": "1",
      "deerflow.auth.remembered_email": "member@example.com",
    });

    expect(loadRememberLoginPreference()).toEqual({
      email: "member@example.com",
      rememberMe: true,
    });
    expect(Object.fromEntries(values)).toEqual({
      "actweave.auth.remember_login": "1",
      "actweave.auth.remembered_email": "member@example.com",
    });
  });

  test("new values win and disabling remember-me deletes every email key", () => {
    const values = installStorage({
      "actweave.auth.remember_login": "1",
      "actweave.auth.remembered_email": "new@example.com",
      "deerflow.auth.remember_login": "1",
      "deerflow.auth.remembered_email": "old@example.com",
    });

    expect(loadRememberLoginPreference().email).toBe("new@example.com");
    saveRememberLoginPreference({
      email: "ignored@example.com",
      rememberMe: false,
    });
    expect(Object.fromEntries(values)).toEqual({
      "actweave.auth.remember_login": "0",
    });
  });
});
