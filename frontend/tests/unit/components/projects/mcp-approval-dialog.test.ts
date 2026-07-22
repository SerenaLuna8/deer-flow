import { describe, expect, test } from "@rstest/core";

import { settleMcpApproval } from "@/components/projects/assets/mcp-approval-dialog";

describe("MCP approval dialog completion", () => {
  test("only closes after an approval reports success", async () => {
    await expect(settleMcpApproval(async () => true)).resolves.toBe(true);
    await expect(settleMcpApproval(async () => false)).resolves.toBe(false);
  });

  test("keeps the dialog open when approval rejects", async () => {
    await expect(
      settleMcpApproval(async () => {
        throw new Error("conflict");
      }),
    ).resolves.toBe(false);
  });
});
