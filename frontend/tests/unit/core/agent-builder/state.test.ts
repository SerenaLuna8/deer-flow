import { describe, expect, test } from "@rstest/core";

import {
  agentBuilderCanRead,
  agentBuilderCancelActionDisabled,
  agentBuilderSlugErrorCode,
  normalizeAgentBuilderSlug,
  prepareAgentBuilderCancelSession,
} from "@/core/agent-builder/state";
import {
  agentBuilderTurnInputSchema,
  type AgentBuilderSession,
} from "@/core/agent-builder/types";

const SESSION = {
  status: "generating",
} as AgentBuilderSession;

describe("Agent Builder state", () => {
  test("accepts only default or an exact model UUID for generation", () => {
    const turn = {
      input: { kind: "message", message: "Design a reviewer" },
      expected_revision: 1,
      idempotency_key: "turn-key",
    } as const;

    expect(
      agentBuilderTurnInputSchema.safeParse({
        ...turn,
        generation_model_ref: "default",
      }).success,
    ).toBe(true);
    expect(
      agentBuilderTurnInputSchema.safeParse({
        ...turn,
        generation_model_ref: "00000000-0000-4000-8000-000000000204",
      }).success,
    ).toBe(true);
    expect(
      agentBuilderTurnInputSchema.safeParse({
        ...turn,
        generation_model_ref: "gpt-5.6-luna",
      }).success,
    ).toBe(false);
    expect(
      agentBuilderTurnInputSchema.safeParse({
        ...turn,
        generation_model_ref: "00000000-0000-4000-8000-00000000020A",
      }).success,
    ).toBe(false);
  });

  test("normalizes and validates a replacement slug with the create rules", () => {
    expect(normalizeAgentBuilderSlug("  Available Agent 2  ")).toBe(
      "available-agent-2",
    );
    expect(agentBuilderSlugErrorCode("available-agent-2")).toBeNull();
    expect(agentBuilderSlugErrorCode("ab")).toBe("too-short");
  });

  test("allows shared-asset readers to inspect a Builder session", () => {
    expect(agentBuilderCanRead(["shared_assets.read"])).toBe(true);
    expect(agentBuilderCanRead(["shared_assets.edit"])).toBe(false);
  });

  test("keeps cancel available while a generation request is pending", () => {
    expect(
      agentBuilderCancelActionDisabled(SESSION, {
        generationPending: true,
        commitPending: false,
        cancelPending: false,
        cancelPreparing: false,
      }),
    ).toBe(false);
  });

  test("refreshes a generating session before choosing the cancel revision", async () => {
    const cached = { ...SESSION, revision: 4 } as AgentBuilderSession;
    const authoritative = { ...SESSION, revision: 5 } as AgentBuilderSession;
    let refetchCalls = 0;

    await expect(
      prepareAgentBuilderCancelSession(cached, true, async () => {
        refetchCalls += 1;
        return authoritative;
      }),
    ).resolves.toBe(authoritative);
    expect(refetchCalls).toBe(1);
  });

  test("blocks cancel during commit and after a terminal state", () => {
    expect(
      agentBuilderCancelActionDisabled(SESSION, {
        generationPending: false,
        commitPending: true,
        cancelPending: false,
        cancelPreparing: false,
      }),
    ).toBe(true);
    expect(
      agentBuilderCancelActionDisabled(
        { ...SESSION, status: "cancelled" },
        {
          generationPending: false,
          commitPending: false,
          cancelPending: false,
          cancelPreparing: false,
        },
      ),
    ).toBe(true);
  });
});
