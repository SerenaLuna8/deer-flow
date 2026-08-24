import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  useParams: () => ({ project_slug: "alpha" }),
}));

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageGroup } from "@/components/workspace/messages/message-group";
import { I18nProvider } from "@/core/i18n/context";

function render(
  messages: Message[],
  {
    includeNarration = true,
    showAllSteps = true,
  }: { includeNarration?: boolean; showAllSteps?: boolean } = {},
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <StandaloneArtifactsProvider enabled={false}>
        <MessageGroup
          messages={messages}
          includeNarration={includeNarration}
          showAllSteps={showAllSteps}
        />
      </StandaloneArtifactsProvider>
    </I18nProvider>,
  );
}

function occurrenceCount(value: string, needle: string) {
  return value.split(needle).length - 1;
}

describe("MessageGroup process narration", () => {
  test("renders every thought and narrated output independently in model-call order", () => {
    const html = render([
      {
        id: "round-1",
        type: "ai",
        content: "OUTPUT_1",
        additional_kwargs: { reasoning_content: "THOUGHT_1" },
        tool_calls: [
          {
            id: "call-1",
            name: "web_search",
            args: { query: "TOOL_1" },
          },
        ],
      },
      {
        id: "call-1-result",
        type: "tool",
        name: "web_search",
        tool_call_id: "call-1",
        content: "[]",
      },
      {
        id: "round-2",
        type: "ai",
        content: "OUTPUT_2",
        additional_kwargs: { reasoning_content: "THOUGHT_2" },
        tool_calls: [
          {
            id: "call-2",
            name: "read_file",
            args: { path: "/tmp/input.md", description: "TOOL_2" },
          },
        ],
      },
      {
        id: "call-2-result",
        type: "tool",
        name: "read_file",
        tool_call_id: "call-2",
        content: "ok",
      },
    ] as Message[]);

    for (const marker of [
      "THOUGHT_1",
      "OUTPUT_1",
      "TOOL_1",
      "THOUGHT_2",
      "OUTPUT_2",
      "TOOL_2",
    ]) {
      expect(occurrenceCount(html, marker)).toBe(1);
    }
    expect(occurrenceCount(html, 'data-testid="thinking-disclosure"')).toBe(2);

    const orderedMarkers = [
      "THOUGHT_1",
      "OUTPUT_1",
      "TOOL_1",
      "THOUGHT_2",
      "OUTPUT_2",
      "TOOL_2",
    ];
    for (let index = 1; index < orderedMarkers.length; index += 1) {
      expect(html.indexOf(orderedMarkers[index - 1]!)).toBeLessThan(
        html.indexOf(orderedMarkers[index]!),
      );
    }
  });

  test("lets a specialized owner suppress narration without suppressing reasoning", () => {
    const html = render(
      [
        {
          id: "present-files-round",
          type: "ai",
          content: "PRESENT_FILES_TRANSITION",
          additional_kwargs: {
            reasoning_content: "PRESENT_FILES_THOUGHT",
          },
          tool_calls: [
            {
              id: "present-files-call",
              name: "present_files",
              args: { files: ["outputs/report.md"] },
            },
          ],
        },
      ] as Message[],
      { includeNarration: false },
    );

    expect(html).toContain("PRESENT_FILES_THOUGHT");
    expect(html).not.toContain("PRESENT_FILES_TRANSITION");
  });

  test("keeps narration visible while only the thought body is collapsed", () => {
    const html = render(
      [
        {
          id: "collapsed-thought-round",
          type: "ai",
          content: "VISIBLE_PROCESS_OUTPUT",
          additional_kwargs: {
            reasoning_content: "COLLAPSED_THOUGHT_BODY",
          },
          tool_calls: [
            {
              id: "collapsed-thought-tool",
              name: "read_file",
              args: {
                path: "/tmp/input.md",
                description: "VISIBLE_TOOL_STEP",
              },
            },
          ],
        },
      ] as Message[],
      { showAllSteps: false },
    );

    expect(html).toContain("VISIBLE_PROCESS_OUTPUT");
    expect(html).toContain("VISIBLE_TOOL_STEP");
    expect(html).toContain('data-testid="thinking-disclosure"');
    expect(html).not.toContain("COLLAPSED_THOUGHT_BODY");
    expect(html).not.toContain("查看其他");
  });
});
