import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, it } from "@rstest/core";

import { indexToolCallData } from "@/core/messages/tool-call-index";

describe("tool call data indexing", () => {
  it("indexes the first visible result and browser preview by tool call id", () => {
    const messages = [
      {
        id: "result-1",
        type: "tool",
        tool_call_id: "call-1",
        content: '{"ok":true}',
        additional_kwargs: {
          browser_view: {
            screenshot: "data:image/png;base64,abc",
            url: "https://example.com",
            title: "Example",
          },
        },
      },
      {
        id: "result-duplicate",
        type: "tool",
        tool_call_id: "call-1",
        content: "later duplicate",
      },
    ] as Message[];

    const indexed = indexToolCallData(messages);

    expect(indexed.toolCallResults.get("call-1")).toBe('{"ok":true}');
    expect(indexed.browserViews.get("call-1")).toEqual({
      screenshot: "data:image/png;base64,abc",
      url: "https://example.com",
      title: "Example",
    });
  });
});
