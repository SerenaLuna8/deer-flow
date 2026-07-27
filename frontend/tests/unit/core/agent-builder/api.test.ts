import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import {
  cancelAgentBuilderSession,
  finalizeAgentBuilderSession,
  createAgentBuilderSession,
  getAgentBuilderSession,
  listAgentBuilderSessions,
  submitAgentBuilderTurn,
} from "@/core/agent-builder/api";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";

const mockedFetch = rs.mocked(fetchWithAuth);
const projectId = "22222222-2222-4222-8222-222222222222";
const sessionId = "11111111-1111-4111-8111-111111111111";
const threadId = "33333333-3333-4333-8333-333333333333";
const assetId = "44444444-4444-4444-8444-444444444444";

const blueprint = {
  description: "负责测试。",
  model_ref: "default",
  tool_groups: [],
  skill_version_ids: [],
  mcp_version_ids: [],
  agents_instructions: "# Agent",
  soul: "# Soul",
  identity: "# Identity",
  user_context: "# User",
};

const agent = {
  id: assetId,
  scope: "project",
  project_id: projectId,
  slug: "test-engineer",
  display_name: "test-engineer",
  status: "active",
  current_published_version_id: "55555555-5555-4555-8555-555555555555",
  version: 3,
  created_by_user_id: "user-1",
  created_at: "2026-07-26T08:01:00Z",
  updated_at: "2026-07-26T08:01:00Z",
};
const session = {
  id: sessionId,
  project_id: projectId,
  owner_user_id: "user-1",
  thread_id: threadId,
  slug: "test-engineer",
  display_name: "test-engineer",
  status: "proposal_ready",
  revision: 3,
  blueprint,
  blueprint_checksum: "checksum-1",
  messages: [],
  active_clarification: null,
  progress: [],
  error_code: null,
  error_message: null,
  created_agent_id: null,
  created_at: "2026-07-26T07:58:00Z",
  updated_at: "2026-07-26T08:00:00Z",
};

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("agent builder api", () => {
  test("uses only project-scoped session routes and strict request bodies", async () => {
    mockedFetch
      .mockResolvedValueOnce(response({ data: session, request_id: "create" }))
      .mockResolvedValueOnce(response({ data: session, request_id: "get" }))
      .mockResolvedValueOnce(
        response({
          data: [
            {
              id: sessionId,
              slug: session.slug,
              display_name: session.display_name,
              status: session.status,
              updated_at: session.updated_at,
            },
          ],
          request_id: "list",
        }),
      )
      .mockResolvedValueOnce(response({ data: session, request_id: "message" }))
      .mockResolvedValueOnce(response({ data: session, request_id: "update" }))
      .mockResolvedValueOnce(
        response({
          data: {
            session: {
              ...session,
              status: "completed",
              created_agent_id: assetId,
            },
            agent,
          },
          request_id: "complete",
        }),
      )
      .mockResolvedValueOnce(
        response({
          data: { ...session, status: "cancelled" },
          request_id: "cancel",
        }),
      );

    await createAgentBuilderSession(projectId, {
      slug: session.slug,
      display_name: session.display_name,
      idempotency_key: "create-key",
    });
    await getAgentBuilderSession(projectId, sessionId);
    await listAgentBuilderSessions(projectId);
    await submitAgentBuilderTurn(projectId, sessionId, {
      input: {
        kind: "message",
        message: "它负责单元测试和集成测试。",
      },
      expected_revision: 3,
      idempotency_key: "message-key",
    });
    await submitAgentBuilderTurn(projectId, sessionId, {
      input: { kind: "blueprint_update", blueprint },
      expected_revision: 3,
      idempotency_key: "blueprint-key",
    });
    await finalizeAgentBuilderSession(projectId, sessionId, {
      expected_revision: 3,
      expected_blueprint_checksum: "checksum-1",
      idempotency_key: "complete-key",
    });
    await cancelAgentBuilderSession(projectId, sessionId, {
      expected_revision: 3,
      idempotency_key: "cancel-key",
    });

    const base = `/backend/api/projects/${projectId}/agent-builder/sessions`;
    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      base,
      `${base}/${sessionId}`,
      base,
      `${base}/${sessionId}/turns`,
      `${base}/${sessionId}/turns`,
      `${base}/${sessionId}/commit`,
      `${base}/${sessionId}/cancel`,
    ]);
    expect(mockedFetch.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          slug: session.slug,
          display_name: session.display_name,
          idempotency_key: "create-key",
        }),
      }),
    );
    expect(mockedFetch.mock.calls[4]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          input: { kind: "blueprint_update", blueprint },
          expected_revision: 3,
          idempotency_key: "blueprint-key",
        }),
      }),
    );
    expect(mockedFetch.mock.calls[6]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          expected_revision: 3,
          idempotency_key: "cancel-key",
        }),
      }),
    );
  });

  test("rejects malformed successful responses instead of trusting them", async () => {
    mockedFetch.mockResolvedValueOnce(
      response({
        data: { ...session, private_prompt: "must not be accepted" },
        request_id: "bad",
      }),
    );
    await expect(getAgentBuilderSession(projectId, sessionId)).rejects.toThrow();
  });
});
