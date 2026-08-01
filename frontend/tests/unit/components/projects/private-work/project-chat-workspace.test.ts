import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, rs, test } from "@rstest/core";
import {
  Children,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  filterProjectConversationThreads,
  projectConversationTitle,
  projectConversationPermissions,
} from "@/components/projects/private-work/project-conversation-rail";
import {
  ProjectThreadDeleteDialog,
  projectThreadDeleteLandingPath,
} from "@/components/projects/private-work/project-thread-delete-dialog";
import { ProjectThreadRenameDialog } from "@/components/projects/private-work/project-thread-rename-dialog";
import { resolveChatRightPanel } from "@/components/workspace/chats/chat-box";
import { shouldShowThreadWelcome } from "@/components/workspace/chats/scoped-chat-page";
import {
  resolveThreadAgentIdentity,
  ThreadAgentIndicator,
} from "@/components/workspace/thread-agent-indicator";
import type { Project } from "@/core/projects/types";
import type { AgentThread } from "@/core/threads";

function thread(threadId: string, title: string): AgentThread {
  return {
    thread_id: threadId,
    created_at: "2026-07-21T00:00:00Z",
    updated_at: "2026-07-21T00:00:00Z",
    state_updated_at: "2026-07-21T00:00:00Z",
    metadata: {},
    status: "idle",
    values: { title, messages: [] },
    interrupts: {},
  };
}

type InspectableElement = ReactElement<
  Record<string, unknown> & { children?: ReactNode }
>;

function descendants(node: ReactNode): InspectableElement[] {
  if (
    !isValidElement<Record<string, unknown> & { children?: ReactNode }>(node)
  ) {
    return [];
  }
  return [
    node,
    ...Children.toArray(node.props.children).flatMap((child) =>
      descendants(child),
    ),
  ];
}

const project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha team",
  display_name: "Alpha Team",
  description: "",
  icon: "folder",
  role: "runner",
  capabilities: [
    "project.read",
    "private_work.read_own",
    "private_work.create",
    "shared_assets.execute",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 1,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "req-project-chat-workspace",
} as Project;

describe("project chat workspace", () => {
  test("filters the persistent conversation rail by normalized title", () => {
    const threads = [
      thread("thread-1", "Release Research"),
      thread("thread-2", "Design review"),
    ];

    expect(
      filterProjectConversationThreads(threads, "  RELEASE  ").map(
        ({ thread_id }) => thread_id,
      ),
    ).toEqual(["thread-1"]);
    expect(filterProjectConversationThreads(threads, "  ")).toEqual(threads);
  });

  test("uses a real empty-state label instead of rendering a blank title", () => {
    const untitled = thread("thread-empty", "");

    expect(projectConversationTitle(untitled)).toBe("新对话");
    expect(filterProjectConversationThreads([untitled], "新对话")).toEqual([
      untitled,
    ]);
  });

  test("keeps create and delete controls capability-driven", () => {
    expect(projectConversationPermissions(project)).toEqual({
      canCreate: true,
      canDelete: true,
      canRename: true,
    });
    expect(
      projectConversationPermissions({
        ...project,
        capabilities: ["project.read", "private_work.read_own"],
      }),
    ).toEqual({ canCreate: false, canDelete: true, canRename: false });
  });

  test("returns to the encoded Chats landing only after deleting the active thread", () => {
    expect(
      projectThreadDeleteLandingPath("alpha team", "thread-1", "thread-1"),
    ).toBe("/projects/alpha%20team/chats");
    expect(
      projectThreadDeleteLandingPath("alpha team", "thread-1", "thread-2"),
    ).toBeNull();
  });

  test("cancel closes the delete confirmation without invoking deletion", () => {
    const onOpenChange = rs.fn();
    const onConfirm = rs.fn();
    const dialog = ProjectThreadDeleteDialog({
      open: true,
      title: "Release Research",
      pending: false,
      onOpenChange,
      onConfirm,
    });
    const elements = descendants(dialog);
    const cancel = elements.find(
      (element) => element.props.children === "取消",
    );
    const confirm = elements.find(
      (element) =>
        element.props["data-testid"] === "project-thread-delete-confirm",
    );

    expect(cancel).toBeDefined();
    expect(confirm).toBeDefined();
    (cancel?.props.onClick as (() => void) | undefined)?.();
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(onConfirm).not.toHaveBeenCalled();

    (confirm?.props.onClick as (() => void) | undefined)?.();
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  test("trims a manual conversation title before saving", () => {
    const onConfirm = rs.fn();
    const dialog = ProjectThreadRenameDialog({
      open: true,
      value: "  Release plan  ",
      pending: false,
      onValueChange: rs.fn(),
      onOpenChange: rs.fn(),
      onConfirm,
    });
    const elements = descendants(dialog);
    const form = elements.find((element) => element.type === "form");

    expect(form).toBeDefined();
    (
      form?.props.onSubmit as
        | ((event: { preventDefault: () => void }) => void)
        | undefined
    )?.({ preventDefault: rs.fn() });
    expect(onConfirm).toHaveBeenCalledWith("Release plan");
  });

  test("treats an empty pre-created project Thread as the welcome state", () => {
    expect(
      shouldShowThreadWelcome({
        isNewThread: false,
        isHistoryLoading: false,
        hasMoreHistory: false,
        visibleMessageCount: 0,
        dismissed: false,
      }),
    ).toBe(true);
    expect(
      shouldShowThreadWelcome({
        isNewThread: false,
        isHistoryLoading: true,
        hasMoreHistory: false,
        visibleMessageCount: 0,
        dismissed: false,
      }),
    ).toBe(false);
    expect(
      shouldShowThreadWelcome({
        isNewThread: false,
        isHistoryLoading: false,
        hasMoreHistory: false,
        visibleMessageCount: 1,
        dismissed: false,
      }),
    ).toBe(false);
    expect(
      shouldShowThreadWelcome({
        isNewThread: true,
        isHistoryLoading: false,
        hasMoreHistory: false,
        visibleMessageCount: 0,
        dismissed: true,
      }),
    ).toBe(false);
  });

  test("resolves the exact Agent identity for existing conversations", () => {
    const catalog = {
      project_items: [
        {
          id: "22222222-2222-4222-8222-222222222222",
          display_name: "Research Agent",
          status: "suspended",
        },
      ],
      system_items: [
        {
          id: "33333333-3333-4333-8333-333333333333",
          display_name: "Main",
          binding: { enabled: false },
        },
      ],
    };

    expect(
      resolveThreadAgentIdentity(
        {
          metadata: {
            agent_asset_id: "22222222-2222-4222-8222-222222222222",
            agent_scope: "project",
          },
        },
        catalog,
        true,
      ),
    ).toEqual({ displayName: "Research Agent", available: true });
    expect(
      resolveThreadAgentIdentity(
        {
          metadata: {
            agent_asset_id: "33333333-3333-4333-8333-333333333333",
            agent_scope: "system",
          },
        },
        catalog,
        true,
      ),
    ).toEqual({ displayName: "Main", available: true });
    expect(
      resolveThreadAgentIdentity(
        {
          metadata: {
            agent_asset_id: "22222222-2222-4222-8222-222222222222",
            agent_scope: "system",
          },
        },
        catalog,
        true,
      ),
    ).toEqual({ displayName: null, available: false });
  });

  test("renders a safe Agent indicator without exposing an unavailable asset id", () => {
    const indicator = ThreadAgentIndicator({
      identity: { displayName: null, available: false },
    });
    const elements = descendants(indicator);
    const root = elements.find(
      (element) => element.props["data-testid"] === "thread-agent-indicator",
    );

    expect(root?.props["aria-label"]).toBe("当前 Agent：不可用");
    expect(renderToStaticMarkup(indicator)).not.toContain(
      "22222222-2222-4222-8222-222222222222",
    );
  });

  test("keeps Files and Sidecar in one mutually exclusive right-panel slot", () => {
    expect(
      resolveChatRightPanel({
        sidecarOpen: true,
        artifactsEnabled: true,
        artifactsOpen: true,
        hasArtifacts: true,
        staticWebsiteOnly: false,
      }),
    ).toBe("sidecar");
    expect(
      resolveChatRightPanel({
        sidecarOpen: false,
        artifactsEnabled: true,
        artifactsOpen: true,
        hasArtifacts: true,
        staticWebsiteOnly: false,
      }),
    ).toBe("artifacts");
    expect(
      resolveChatRightPanel({
        sidecarOpen: false,
        artifactsEnabled: true,
        artifactsOpen: false,
        hasArtifacts: true,
        staticWebsiteOnly: false,
      }),
    ).toBeNull();
    expect(
      resolveChatRightPanel({
        sidecarOpen: false,
        artifactsEnabled: true,
        artifactsOpen: true,
        hasArtifacts: false,
        staticWebsiteOnly: true,
      }),
    ).toBeNull();
  });

  test("switching conversations remounts the transcript and jumps to the bottom", () => {
    const scopedChat = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/chats/scoped-chat-page.tsx",
      ),
      "utf8",
    );
    const mainMessageList =
      /<MessageList[\s\S]*?testId="main-message-list"[\s\S]*?\/>/u.exec(
        scopedChat,
      )?.[0];

    expect(mainMessageList).toBeDefined();
    expect(mainMessageList).toContain("key={threadId}");
    expect(mainMessageList).toContain('initialScroll="instant"');
  });

  test("wires scoped edit replay while keeping the composer available for clarification replies", () => {
    const scopedChat = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/chats/scoped-chat-page.tsx",
      ),
      "utf8",
    );
    const mainMessageList =
      /<MessageList[\s\S]*?testId="main-message-list"[\s\S]*?\/>/u.exec(
        scopedChat,
      )?.[0];
    const inputBox = /<InputBox[\s\S]*?\/>/u.exec(scopedChat)?.[0];

    expect(scopedChat).toContain("editAndRegenerateMessage");
    expect(mainMessageList).toContain("canEdit=");
    expect(mainMessageList).toContain("onEditAndRegenerateMessage=");
    expect(mainMessageList).toContain("!hasOpenHumanInputCard");
    expect(inputBox).toBeDefined();
    expect(inputBox).not.toContain("hasOpenHumanInputCard");
  });

  test("keeps the conversation title out of the top toolbar", () => {
    const scopedChat = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/chats/scoped-chat-page.tsx",
      ),
      "utf8",
    );
    const toolbar = /<header[\s\S]*?<\/header>/u.exec(scopedChat)?.[0];

    expect(toolbar).toBeDefined();
    expect(toolbar).not.toContain("<ThreadTitle");
    expect(scopedChat).toContain("<ThreadDocumentTitle thread={thread} />");
  });

  test("uses the appearance width for the empty conversation composer", () => {
    const scopedChat = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/chats/scoped-chat-page.tsx",
      ),
      "utf8",
    );

    expect(scopedChat).toContain('data-testid="chat-composer-width"');
    expect(scopedChat).not.toContain('"max-w-(--container-width-sm)"');
    expect(scopedChat).toContain("max-w-(--chat-content-width)");
  });

  test("keeps welcome quick actions visually attached to the composer", () => {
    const inputBox = readFileSync(
      resolve(process.cwd(), "src/components/workspace/input-box.tsx"),
      "utf8",
    );

    expect(inputBox).toContain('data-testid="welcome-quick-actions"');
    expect(inputBox).toContain(
      '<Suggestions className="w-full max-w-full justify-center px-4 sm:w-fit sm:px-0">',
    );
    expect(inputBox).not.toContain(
      '<Suggestions className="min-h-16 w-full max-w-full justify-center px-4 sm:w-fit sm:px-0">',
    );
    expect(inputBox).not.toContain('isWelcomeMode ? "gap-4" : "gap-2"');
    expect(inputBox).toContain(
      'className="relative flex min-w-0 flex-col gap-2"',
    );
  });
});
