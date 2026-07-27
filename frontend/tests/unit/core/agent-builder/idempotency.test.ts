import { describe, expect, test } from "@rstest/core";

import {
  agentBuilderSemanticSignature,
  createAgentBuilderIdempotencyRegistry,
} from "@/core/agent-builder/idempotency";

describe("Agent Builder semantic idempotency", () => {
  test("reuses the exact command while the semantic input is unchanged", () => {
    let sequence = 0;
    const registry = createAgentBuilderIdempotencyRegistry(
      () => `key-${++sequence}`,
    );
    const signature = agentBuilderSemanticSignature({
      kind: "message",
      message: "生成测试",
    });
    const first = registry.acquire("message-turn", signature, (key) => ({
      input: { kind: "message" as const, message: "生成测试" },
      expected_revision: 4,
      idempotency_key: key,
    }));
    const retry = registry.acquire("message-turn", signature, (key) => ({
      input: { kind: "message" as const, message: "生成测试" },
      expected_revision: 9,
      idempotency_key: key,
    }));

    expect(retry).toBe(first);
    expect(retry).toEqual({
      input: { kind: "message", message: "生成测试" },
      expected_revision: 4,
      idempotency_key: "key-1",
    });
  });

  test("changes keys for new semantics and ignores late completion of an older operation", () => {
    let sequence = 0;
    const registry = createAgentBuilderIdempotencyRegistry(
      () => `key-${++sequence}`,
    );
    const firstSignature = agentBuilderSemanticSignature({
      kind: "message",
      message: "第一版",
    });
    const secondSignature = agentBuilderSemanticSignature({
      kind: "message",
      message: "第二版",
    });
    const first = registry.acquire(
      "message-turn",
      firstSignature,
      (key) => key,
    );
    const second = registry.acquire(
      "message-turn",
      secondSignature,
      (key) => key,
    );

    registry.complete("message-turn", firstSignature);
    expect(
      registry.acquire("message-turn", secondSignature, (key) => key),
    ).toBe(second);

    registry.complete("message-turn", secondSignature);
    expect(
      registry.acquire("message-turn", secondSignature, (key) => key),
    ).not.toBe(second);
    expect([first, second, sequence]).toEqual(["key-1", "key-2", 3]);
  });

  test("canonicalizes object key order for the same semantic payload", () => {
    expect(
      agentBuilderSemanticSignature({
        response: { value: "执行", option_id: "execute" },
        kind: "clarification",
      }),
    ).toBe(
      agentBuilderSemanticSignature({
        kind: "clarification",
        response: { option_id: "execute", value: "执行" },
      }),
    );
  });
});
