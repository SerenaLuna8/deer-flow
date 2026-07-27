import { describe, expect, test } from "@rstest/core";

import {
  createSkillBuilderIdempotencyRegistry,
  skillBuilderSemanticSignature,
} from "@/core/skill-builder/idempotency";

describe("skill builder idempotency", () => {
  test("reuses a command only while its semantic operation is unresolved", () => {
    let sequence = 0;
    const registry = createSkillBuilderIdempotencyRegistry(
      () => `key-${++sequence}`,
    );
    const signature = skillBuilderSemanticSignature({
      changes: [{ path: "SKILL.md", content: "# Skill" }],
    });
    const first = registry.acquire("draft-turn", signature, (key) => ({ key }));
    const retry = registry.acquire("draft-turn", signature, (key) => ({ key }));
    expect(retry).toBe(first);
    registry.complete("draft-turn", signature);
    expect(
      registry.acquire("draft-turn", signature, (key) => ({ key })),
    ).toEqual({ key: "key-2" });
  });
});
