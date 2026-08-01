import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  cancelSkillBuilderSession,
  commitSkillBuilderSession,
  createSkillBuilderSession,
  getSkillBuilderSession,
  listSkillBuilderSessions,
  submitSkillBuilderTurn,
  type SkillBuilderApiError,
  validateSkillBuilderSession,
} from "@/core/skill-builder/api";

const mockedFetch = rs.mocked(fetchWithAuth);
const projectId = "22222222-2222-4222-8222-222222222222";
const sessionId = "11111111-1111-4111-8111-111111111111";
const threadId = "33333333-3333-4333-8333-333333333333";
const skillId = "44444444-4444-4444-8444-444444444444";

const session = {
  id: sessionId,
  project_id: projectId,
  owner_user_id: "user-1",
  thread_id: threadId,
  slug: "paper-review",
  display_name: "paper-review",
  status: "draft_ready",
  revision: 3,
  messages: [],
  active_clarification: null,
  progress: [],
  files: [
    {
      path: "SKILL.md",
      media_type: "text/markdown",
      size_bytes: 7,
      sha256: "a".repeat(64),
      encoding: "utf-8",
      content: "# Skill",
    },
  ],
  draft_checksum: "c".repeat(64),
  validation: null,
  error_code: null,
  error_message: null,
  created_skill_id: null,
  created_at: "2026-07-27T07:58:00Z",
  updated_at: "2026-07-27T08:00:00Z",
};

const skill = {
  id: skillId,
  scope: "project",
  project_id: projectId,
  slug: "paper-review",
  display_name: "paper-review",
  status: "suspended",
  current_published_version_id: "55555555-5555-4555-8555-555555555555",
  version: 1,
  created_by_user_id: "user-1",
  created_at: "2026-07-27T08:01:00Z",
  updated_at: "2026-07-27T08:01:00Z",
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

describe("skill builder api", () => {
  test("uses only project-scoped Builder routes and checksum-bound writes", async () => {
    mockedFetch
      .mockResolvedValueOnce(response({ data: session, request_id: "create" }))
      .mockResolvedValueOnce(response({ data: session, request_id: "get" }))
      .mockResolvedValueOnce(response({ data: [], request_id: "list" }))
      .mockResolvedValueOnce(response({ data: session, request_id: "turn" }))
      .mockResolvedValueOnce(
        response({
          data: {
            ...session,
            status: "validated",
            validation: {
              draft_checksum: session.draft_checksum,
              validated_at: "2026-07-27T08:01:00Z",
              description: "Review papers",
              frontmatter: { name: "paper-review" },
              compatibility: null,
              secret_requirements: [],
              scan_decision: "allow",
              scan_rule_ids: [],
              scan_summary: {},
            },
          },
          request_id: "validate",
        }),
      )
      .mockResolvedValueOnce(
        response({
          data: {
            session: {
              ...session,
              status: "completed",
              created_skill_id: skillId,
            },
            skill,
          },
          request_id: "commit",
        }),
      )
      .mockResolvedValueOnce(
        response({
          data: { ...session, status: "cancelled" },
          request_id: "cancel",
        }),
      );

    await createSkillBuilderSession(projectId, {
      slug: session.slug,
      display_name: session.display_name,
      idempotency_key: "create-key",
    });
    await getSkillBuilderSession(projectId, sessionId);
    await listSkillBuilderSessions(projectId);
    await submitSkillBuilderTurn(projectId, sessionId, {
      input: {
        kind: "draft_update",
        expected_draft_checksum: "c".repeat(64),
        changes: [
          {
            op: "replace",
            path: "SKILL.md",
            content: "# Updated",
            media_type: "text/markdown",
          },
        ],
      },
      expected_revision: 3,
      idempotency_key: "draft-key",
    });
    await validateSkillBuilderSession(projectId, sessionId, {
      expected_revision: 3,
      expected_draft_checksum: "c".repeat(64),
      idempotency_key: "validate-key",
    });
    await commitSkillBuilderSession(projectId, sessionId, {
      expected_revision: 3,
      expected_draft_checksum: "c".repeat(64),
      acknowledge_warnings: false,
      idempotency_key: "commit-key",
    });
    await cancelSkillBuilderSession(projectId, sessionId, {
      expected_revision: 3,
      idempotency_key: "cancel-key",
    });

    const base = `/backend/api/projects/${projectId}/skill-builder/sessions`;
    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      base,
      `${base}/${sessionId}`,
      base,
      `${base}/${sessionId}/turns`,
      `${base}/${sessionId}/validate`,
      `${base}/${sessionId}/commit`,
      `${base}/${sessionId}/cancel`,
    ]);
    expect(mockedFetch.mock.calls[3]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          input: {
            kind: "draft_update",
            expected_draft_checksum: "c".repeat(64),
            changes: [
              {
                op: "replace",
                path: "SKILL.md",
                content: "# Updated",
                media_type: "text/markdown",
              },
            ],
          },
          expected_revision: 3,
          idempotency_key: "draft-key",
        }),
      }),
    );
  });

  test("rejects unknown successful response fields", async () => {
    mockedFetch.mockResolvedValueOnce(
      response({
        data: { ...session, storage_locator: "private" },
        request_id: "bad",
      }),
    );
    await expect(
      getSkillBuilderSession(projectId, sessionId),
    ).rejects.toThrow();
  });

  test("classifies the unfinished-session capacity response", async () => {
    mockedFetch.mockResolvedValueOnce(
      response(
        {
          detail: {
            code: "asset_storage_quota_exceeded",
            message: "Project Skill storage quota exceeded",
          },
        },
        429,
      ),
    );

    await expect(
      createSkillBuilderSession(projectId, {
        slug: session.slug,
        display_name: session.display_name,
        idempotency_key: "capacity-key",
      }),
    ).rejects.toMatchObject({
      status: 429,
      code: "SKILL_BUILDER_LIMIT_EXCEEDED",
    } satisfies Partial<SkillBuilderApiError>);
  });

  test("classifies a non-JSON gateway timeout as unavailable", async () => {
    mockedFetch.mockResolvedValueOnce(
      new Response("<html><title>504 Gateway Time-out</title></html>", {
        status: 504,
        headers: { "Content-Type": "text/html" },
      }),
    );

    await expect(
      submitSkillBuilderTurn(projectId, sessionId, {
        input: { kind: "message", message: "写代码" },
        expected_revision: 3,
        idempotency_key: "timeout-key",
      }),
    ).rejects.toMatchObject({
      status: 504,
      code: "SKILL_BUILDER_UNAVAILABLE",
    } satisfies Partial<SkillBuilderApiError>);
  });
});
