import { describe, expect, test } from "@rstest/core";

import {
  mcpApprovalCopy,
  settleMcpApproval,
} from "@/components/projects/assets/mcp-approval-dialog";

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

  test("uses grant-only wording for packaged System MCP configuration", () => {
    const copy = mcpApprovalCopy("configure-grants");

    expect(copy.title).toBe("配置 MCP Credential 授权");
    expect(copy.description).toContain("只配置槽位授权");
    expect(copy.submitLabel).toBe("保存授权");
    expect(copy.emptyOptionalMessage).toContain("清除既有授权");
    expect(copy.description).not.toContain("批准成功后版本才会发布");
  });
});
