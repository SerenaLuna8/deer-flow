import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  useParams: () => ({ project_slug: "alpha" }),
}));

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageGroup } from "@/components/workspace/messages/message-group";
import { I18nProvider } from "@/core/i18n/context";

function render(messages: Message[], { showAllSteps = false } = {}) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <StandaloneArtifactsProvider enabled={false}>
        <MessageGroup
          messages={messages}
          showAllSteps={showAllSteps}
          renderTaskToolCall={(taskId) => (
            <div data-testid="subtask-card">SUBTASK_CARD_{taskId}</div>
          )}
        />
      </StandaloneArtifactsProvider>
    </I18nProvider>,
  );
}

function occurrenceCount(value: string, needle: string) {
  return value.split(needle).length - 1;
}

describe("MessageGroup Sub-Agent visibility", () => {
  test("keeps every task visible while folding only the preceding reasoning step", () => {
    const html = render([
      {
        type: "ai",
        id: "subagent-round",
        content: "",
        additional_kwargs: {
          reasoning_content: "REASONING_THAT_STARTS_THE_DELEGATION",
        },
        tool_calls: [
          { id: "task-1", name: "task", args: { description: "First" } },
          { id: "task-2", name: "task", args: { description: "Second" } },
          { id: "task-3", name: "task", args: { description: "Third" } },
        ],
      },
    ]);

    expect(html).toContain("查看其他 1 个步骤");
    expect(html).not.toContain("查看其他 3 个步骤");
    expect(html).not.toContain("REASONING_THAT_STARTS_THE_DELEGATION");

    const taskMarkers = [
      "SUBTASK_CARD_task-1",
      "SUBTASK_CARD_task-2",
      "SUBTASK_CARD_task-3",
    ];
    for (const marker of taskMarkers) {
      expect(occurrenceCount(html, marker)).toBe(1);
    }
    expect(html.indexOf(taskMarkers[0]!)).toBeLessThan(
      html.indexOf(taskMarkers[1]!),
    );
    expect(html.indexOf(taskMarkers[1]!)).toBeLessThan(
      html.indexOf(taskMarkers[2]!),
    );
  });

  test("renders the same reasoning and three tasks exactly once in canonical order when expanded", () => {
    const html = render(
      [
        {
          type: "ai",
          id: "expanded-subagent-round",
          content: "",
          additional_kwargs: {
            reasoning_content: "EXPANDED_DELEGATION_REASONING",
          },
          tool_calls: [
            { id: "expanded-task-1", name: "task", args: {} },
            { id: "expanded-task-2", name: "task", args: {} },
            { id: "expanded-task-3", name: "task", args: {} },
          ],
        },
      ],
      { showAllSteps: true },
    );

    expect(html).toContain("EXPANDED_DELEGATION_REASONING");
    expect(html).not.toContain("查看其他");

    const taskMarkers = [
      "SUBTASK_CARD_expanded-task-1",
      "SUBTASK_CARD_expanded-task-2",
      "SUBTASK_CARD_expanded-task-3",
    ];
    for (const marker of taskMarkers) {
      expect(occurrenceCount(html, marker)).toBe(1);
    }
    expect(html.indexOf(taskMarkers[0]!)).toBeLessThan(
      html.indexOf(taskMarkers[1]!),
    );
    expect(html.indexOf(taskMarkers[1]!)).toBeLessThan(
      html.indexOf(taskMarkers[2]!),
    );
  });

  test("keeps task cards ordered and unique when present_files closes the tool round", () => {
    const html = render([
      {
        type: "ai",
        id: "present-files-round",
        content: "",
        additional_kwargs: {
          reasoning_content: "REASONING_BEFORE_TASK_AND_FILES",
        },
        tool_calls: [
          { id: "task-a", name: "task", args: { description: "First" } },
          {
            id: "search-between-tasks",
            name: "web_search",
            args: { query: "agent history" },
          },
          { id: "task-b", name: "task", args: { description: "Second" } },
          {
            id: "present-files",
            name: "present_files",
            args: { files: ["outputs/report.md"] },
          },
        ],
      },
    ]);

    expect(html).toContain("查看其他 2 个步骤");
    expect(html).not.toContain("REASONING_BEFORE_TASK_AND_FILES");
    expect(html).not.toContain("agent history");

    const firstTask = "SUBTASK_CARD_task-a";
    const secondTask = "SUBTASK_CARD_task-b";
    expect(occurrenceCount(html, firstTask)).toBe(1);
    expect(occurrenceCount(html, secondTask)).toBe(1);
    expect(html.indexOf(firstTask)).toBeLessThan(html.indexOf(secondTask));
  });
});
