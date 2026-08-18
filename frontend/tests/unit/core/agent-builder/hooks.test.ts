import { describe, expect, test } from "@rstest/core";

import {
  agentBuilderPollingInterval,
  newestAgentBuilderSession,
} from "@/core/agent-builder/hooks";
import type { AgentBuilderSession } from "@/core/agent-builder/types";

describe("Agent Builder query reconciliation", () => {
  test("keeps polling while a turn request is pending before the server exposes generating", () => {
    expect(
      agentBuilderPollingInterval(
        { status: "interviewing" } as AgentBuilderSession,
        { canAuthor: true, requestPending: true },
      ),
    ).toBe(1_000);
  });

  test("does not continuously poll stale in-progress sessions for read-only viewers", () => {
    expect(
      agentBuilderPollingInterval(
        { status: "generating" } as AgentBuilderSession,
        { canAuthor: false },
      ),
    ).toBe(false);
    expect(
      agentBuilderPollingInterval(
        { status: "committing" } as AgentBuilderSession,
        { canAuthor: false },
      ),
    ).toBe(false);
  });

  test("continues polling in-progress sessions for Agent editors", () => {
    expect(
      agentBuilderPollingInterval(
        { status: "generating" } as AgentBuilderSession,
        { canAuthor: true },
      ),
    ).toBe(1_000);
    expect(
      agentBuilderPollingInterval(
        { status: "committing" } as AgentBuilderSession,
        { canAuthor: true },
      ),
    ).toBe(1_000);
  });

  test("does not let a late turn response overwrite a newer cancelled revision", () => {
    const cancelled = {
      status: "cancelled",
      revision: 3,
    } as AgentBuilderSession;
    const lateTurn = {
      status: "proposal_ready",
      revision: 2,
    } as AgentBuilderSession;

    expect(newestAgentBuilderSession(cancelled, lateTurn)).toBe(cancelled);
    expect(newestAgentBuilderSession(lateTurn, cancelled)).toBe(cancelled);
  });
});
