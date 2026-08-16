import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageListItem } from "@/components/workspace/messages/message-list-item";
import { I18nProvider } from "@/core/i18n/context";

rs.mock("@/core/workspace-changes/hooks", () => ({
  useWorkspaceChanges: () => ({ data: undefined, isLoading: false }),
}));

function contentTag(html: string, role: "user" | "assistant") {
  return new RegExp(`<div[^>]*data-message-content-role="${role}"[^>]*>`).exec(
    html,
  )?.[0];
}

function renderItem(message: Message) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
      <StandaloneArtifactsProvider enabled={false}>
        <MessageListItem
          message={message}
          showCopyButton={false}
          showReasoning={false}
          threadId="thread-1"
        />
      </StandaloneArtifactsProvider>
    </I18nProvider>,
  );
}

describe("MessageListItem layout", () => {
  test("keeps user messages content-sized, right aligned, and narrower than the timeline", () => {
    const html = renderItem({
      id: "human-1",
      type: "human",
      content: "A short question",
    } as Message);

    expect(html).toContain("is-user ml-auto justify-end");
    expect(contentTag(html, "user")).toContain("ml-auto");
    expect(contentTag(html, "user")).toContain(
      "w-fit max-w-[88%] sm:max-w-[75%]",
    );
  });

  test("keeps assistant messages left aligned and full width", () => {
    const html = renderItem({
      id: "ai-1",
      type: "ai",
      content: "A full-width answer",
      additional_kwargs: {},
    } as Message);

    expect(html).toContain("is-assistant");
    expect(contentTag(html, "assistant")).toContain("w-full");
    expect(contentTag(html, "assistant")).not.toContain("max-w-[75%]");
  });
});
