import { describe, expect, it } from "@rstest/core";

import { syntheticAccount } from "../../e2e-release/support/m8-api";

describe("M8 synthetic account", () => {
  it("uses the backend-accepted reserved example domain", () => {
    const account = syntheticAccount("contract");

    expect(account.email).toMatch(/^m8-contract-[0-9a-f]{32}@example\.com$/u);
    expect(account.password).not.toContain(account.email);
  });
});
