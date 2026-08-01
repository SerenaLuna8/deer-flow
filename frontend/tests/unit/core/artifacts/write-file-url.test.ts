import { describe, expect, test } from "@rstest/core";

import { buildWriteFileArtifactURL } from "@/core/artifacts/utils";

describe("buildWriteFileArtifactURL", () => {
  test("builds a normal write-file URL with stable message identities", () => {
    expect(
      buildWriteFileArtifactURL({
        filepath: "/mnt/user-data/outputs/report.md",
        messageId: "ai-1",
        toolCallId: "call-1",
      }),
    ).toBe(
      "write-file:/mnt/user-data/outputs/report.md?message_id=ai-1&tool_call_id=call-1",
    );
  });

  test("encodes path and query delimiters without changing the logical path", () => {
    const filepath = "/mnt/user-data/outputs/a b#c?%20.md";
    const url = new URL(
      buildWriteFileArtifactURL({
        filepath,
        messageId: "message #1",
        toolCallId: "call ?1",
      }),
    );

    expect(decodeURIComponent(url.pathname)).toBe(filepath);
    expect(url.searchParams.get("message_id")).toBe("message #1");
    expect(url.searchParams.get("tool_call_id")).toBe("call ?1");
  });

  test("does not write undefined identity parameters", () => {
    const url = buildWriteFileArtifactURL({
      filepath: "/mnt/user-data/outputs/report.md",
    });

    expect(url).not.toContain("undefined");
    expect(new URL(url).search).toBe("");
  });
});
