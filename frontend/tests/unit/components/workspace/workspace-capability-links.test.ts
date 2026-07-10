import { describe, expect, it } from "@rstest/core";

import {
  WORKSPACE_CAPABILITY_LINKS,
  isWorkspaceCapabilityPath,
} from "@/components/workspace/workspace-capability-links";

describe("WORKSPACE_CAPABILITY_LINKS", () => {
  it("keeps memory, tools, and skills in the requested order", () => {
    expect(WORKSPACE_CAPABILITY_LINKS).toEqual([
      { id: "memory", href: "/workspace/memory" },
      { id: "tools", href: "/workspace/tools" },
      { id: "skills", href: "/workspace/skills" },
    ]);
  });
});

describe("isWorkspaceCapabilityPath", () => {
  it("matches a capability root and its descendants only", () => {
    expect(
      isWorkspaceCapabilityPath("/workspace/memory", "/workspace/memory"),
    ).toBe(true);
    expect(
      isWorkspaceCapabilityPath("/workspace/memory/fact", "/workspace/memory"),
    ).toBe(true);
    expect(
      isWorkspaceCapabilityPath("/workspace/memory-bank", "/workspace/memory"),
    ).toBe(false);
  });
});
