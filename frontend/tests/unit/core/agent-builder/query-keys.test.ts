import { describe, expect, test } from "@rstest/core";

import {
  agentBuilderSessionKey,
  agentBuilderSessionsInvalidation,
  agentBuilderSessionsKey,
} from "@/core/agent-builder/query-keys";

describe("Agent Builder query keys", () => {
  test("invalidates only the session list after caching a detail response", () => {
    const accountId = "account-1";
    const projectId = "project-1";
    const sessionId = "session-1";

    expect(agentBuilderSessionsInvalidation(accountId, projectId)).toEqual({
      queryKey: agentBuilderSessionsKey(accountId, projectId),
      exact: true,
    });
    expect(agentBuilderSessionKey(accountId, projectId, sessionId)).toEqual([
      ...agentBuilderSessionsKey(accountId, projectId),
      sessionId,
    ]);
  });
});
