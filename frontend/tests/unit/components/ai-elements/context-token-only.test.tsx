import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  Context,
  ContextCacheUsage,
  ContextContentBody,
  ContextContentFooter,
  ContextInputUsage,
  ContextOutputUsage,
  ContextReasoningUsage,
} from "@/components/ai-elements/context";

describe("Context token usage", () => {
  test("renders token counts without monetary output", () => {
    const html = renderToStaticMarkup(
      <Context
        usedTokens={2_000}
        maxTokens={10_000}
        modelId="legacy-model-id"
        usage={{
          inputTokens: 1_200,
          inputTokenDetails: {
            noCacheTokens: 1_100,
            cacheReadTokens: 100,
            cacheWriteTokens: undefined,
          },
          outputTokens: 500,
          outputTokenDetails: {
            textTokens: 300,
            reasoningTokens: 200,
          },
          reasoningTokens: 200,
          cachedInputTokens: 100,
          totalTokens: 2_000,
        }}
      >
        <ContextContentBody>
          <ContextInputUsage />
          <ContextOutputUsage />
          <ContextReasoningUsage />
          <ContextCacheUsage />
        </ContextContentBody>
        <ContextContentFooter>
          Usage is reported in tokens.
        </ContextContentFooter>
      </Context>,
    );

    expect(html).toContain("Input");
    expect(html).toContain("1.2K");
    expect(html).toContain("Output");
    expect(html).toContain("500");
    expect(html).toContain("Reasoning");
    expect(html).toContain("200");
    expect(html).toContain("Cache");
    expect(html).toContain("100");
    expect(html).toContain("Usage is reported in tokens.");
    expect(html).not.toContain("$");
    expect(html.toLowerCase()).not.toContain("cost");
    expect(html).not.toContain("USD");
    expect(html).not.toContain("legacy-model-id");
  });
});
