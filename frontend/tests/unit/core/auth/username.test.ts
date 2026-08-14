import { describe, expect, test } from "@rstest/core";

import { parseUsername } from "@/core/auth/username";

describe("parseUsername", () => {
  test("accepts ascii identifiers and stores them lowercase", () => {
    expect(parseUsername("Admin")).toBe("admin");
    expect(parseUsername("user_01")).toBe("user_01");
    expect(parseUsername("  Alice_1  ")).toBe("alice_1");
  });

  test("rejects chinese, special characters, and invalid shapes", () => {
    for (const raw of [
      "",
      "ab",
      "1admin",
      "用户名",
      "alice.chen",
      "alice-chen",
      "alice@site",
      "alice chen",
      "a".repeat(33),
    ]) {
      expect(parseUsername(raw)).toBeNull();
    }
  });
});
