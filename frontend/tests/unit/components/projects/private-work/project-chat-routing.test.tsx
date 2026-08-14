import { describe, expect, test } from "@rstest/core";

import { projectChatRouteScope } from "@/components/projects/private-work/project-chat-page";
import { projectThreadDeleteLandingPath } from "@/components/projects/private-work/project-thread-delete-dialog";
import type { Project } from "@/core/projects/types";

describe("project chat routing", () => {
  test("exposes only server-created chat routes", () => {
    const scope = projectChatRouteScope({
      slug: "研发/平台",
      capabilities: [
        "private_work.create",
        "private_work.read_own",
        "private_work.approve_host_execution",
        "shared_assets.execute",
      ],
    } satisfies Pick<Project, "slug" | "capabilities">);

    expect(scope.threadBasePath).toBe(
      "/projects/%E7%A0%94%E5%8F%91%2F%E5%B9%B3%E5%8F%B0/chats",
    );
    expect(scope.threadListPath).toBe(scope.threadBasePath);
    expect(scope.canApproveHostExecution).toBe(true);
    expect(scope).not.toHaveProperty("newThreadPath");
    expect(
      Object.values(scope).some(
        (value) => typeof value === "string" && value.endsWith("/new"),
      ),
    ).toBe(false);
  });

  test("derives host execution approval only from the server capability", () => {
    const scope = projectChatRouteScope({
      slug: "alpha",
      capabilities: [
        "private_work.create",
        "private_work.read_own",
        "shared_assets.execute",
      ],
    } satisfies Pick<Project, "slug" | "capabilities">);

    expect(scope.canRun).toBe(true);
    expect(scope.canApproveHostExecution).toBe(false);
  });

  test("returns to the server-backed chat list only after deleting the active thread", () => {
    expect(
      projectThreadDeleteLandingPath("研发/平台", "thread-1", "thread-1"),
    ).toBe("/projects/%E7%A0%94%E5%8F%91%2F%E5%B9%B3%E5%8F%B0/chats");
    expect(
      projectThreadDeleteLandingPath("研发/平台", "thread-1", "thread-2"),
    ).toBeNull();
    expect(
      projectThreadDeleteLandingPath("研发/平台", undefined, "thread-2"),
    ).toBeNull();
  });
});
