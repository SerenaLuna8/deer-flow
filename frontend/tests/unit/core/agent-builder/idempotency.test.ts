import { describe, expect, test } from "@rstest/core";

import {
  agentBuilderSemanticallyEqual,
  agentBuilderSemanticSignature,
  createAgentBuilderIdempotencyRegistry,
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

  test("issues a fresh command after a stale command is released", () => {
    let nextKey = 0;
    const registry = createAgentBuilderIdempotencyRegistry(
      () => `key-${++nextKey}`,
    );
    const signature = agentBuilderSemanticSignature({ message: "review" });
    const first = registry.acquire("message-turn", signature, (key) => ({
      key,
    }));

    registry.complete("message-turn", signature);

    const retried = registry.acquire("message-turn", signature, (key) => ({
      key,
    }));
    expect(first).toEqual({ key: "key-1" });
    expect(retried).toEqual({ key: "key-2" });
  });
});
