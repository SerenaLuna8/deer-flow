import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AssistantActionRow } from "@/components/workspace/messages/assistant-action-row";

describe("AssistantActionRow", () => {
  test("keeps its layout stable while revealing actions on turn hover or focus", () => {
    const markup = renderToStaticMarkup(
      <AssistantActionRow>
        <button type="button">Copy</button>
      </AssistantActionRow>,
    );

    expect(markup).toContain("opacity-0");
    expect(markup).toContain("pointer-events-none");
    expect(markup).toContain("duration-150");
    expect(markup).toContain("group-hover/assistant-turn:opacity-100");
    expect(markup).toContain("group-hover/assistant-turn:pointer-events-auto");
    expect(markup).toContain("group-focus-within/assistant-turn:opacity-100");
    expect(markup).toContain(
      "group-focus-within/assistant-turn:pointer-events-auto",
    );
  });
});
