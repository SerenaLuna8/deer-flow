import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AssistantActionRow } from "@/components/workspace/messages/assistant-action-row";

describe("AssistantActionRow", () => {
  test("does not reserve vertical space until its turn is hovered or focused", () => {
    const markup = renderToStaticMarkup(
      <AssistantActionRow>
        <button type="button">Copy</button>
      </AssistantActionRow>,
    );

    expect(markup).toContain("sr-only");
    expect(markup).toContain("group-hover/assistant-turn:not-sr-only");
    expect(markup).toContain("group-focus-within/assistant-turn:not-sr-only");
  });
});
