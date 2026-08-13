import { describe, expect, test } from "@rstest/core";

import {
  agentBuilderSemanticallyEqual,
  agentBuilderSemanticSignature,
} from "@/core/agent-builder/idempotency";

describe("Agent Builder semantic values", () => {
  test("treats object key order and omitted undefined fields as equivalent", () => {
    const baseline = {
      description: "Review code",
      model: { name: "default", settings: { temperature: 0 } },
      optional: undefined,
    };
    const draft = {
      model: { settings: { temperature: 0 }, name: "default" },
      description: "Review code",
    };

    expect(agentBuilderSemanticallyEqual(baseline, draft)).toBe(true);
    expect(agentBuilderSemanticSignature(baseline)).toBe(
      agentBuilderSemanticSignature(draft),
    );
  });

  test("keeps array order meaningful", () => {
    expect(
      agentBuilderSemanticallyEqual(["read", "task"], ["task", "read"]),
    ).toBe(false);
  });
});
