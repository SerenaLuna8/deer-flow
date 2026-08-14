import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn(),
  useQuery: rs.fn(),
  useQueryClient: rs.fn(),
}));
rs.mock("@/core/private-work/provider", () => ({
  usePrivateWorkAccess: rs.fn(),
}));
rs.mock("@/core/skill-builder/api", () => ({
  cancelSkillBuilderSession: rs.fn(),
  commitSkillBuilderSession: rs.fn(),
  createSkillBuilderRevisionSession: rs.fn(),
  createSkillBuilderSession: rs.fn(),
  getSkillBuilderSession: rs.fn(),
  listSkillBuilderSessions: rs.fn(),
  submitSkillBuilderTurn: rs.fn(),
  validateSkillBuilderSession: rs.fn(),
}));

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { PrivateWorkAccess } from "@/core/private-work/types";
import {
  skillBuilderSessionKey,
  skillBuilderSessionsInvalidation,
  useCreateSkillBuilderRevisionSession,
} from "@/core/skill-builder";
import type { SkillBuilderSession } from "@/core/skill-builder/types";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_ID = "33333333-3333-4333-8333-333333333333";
const NOW = "2026-08-14T08:00:00+08:00";

type MutationOptions = {
  mutationKey: readonly unknown[];
  mutationFn: (input: unknown) => Promise<unknown>;
  onSuccess: (response: { data: SkillBuilderSession }) => void;
};

const mockedUseMutation = rs.mocked(useMutation);
const mockedUseQuery = rs.mocked(useQuery);
const mockedUseQueryClient = rs.mocked(useQueryClient);
const mockedUsePrivateWorkAccess = rs.mocked(usePrivateWorkAccess);

function reviseSession(): SkillBuilderSession {
  return {
    id: SESSION_ID,
    project_id: PROJECT_ID,
    owner_user_id: "owner-1",
    thread_id: "44444444-4444-4444-8444-444444444444",
    slug: "catalog-auditor",
    display_name: "catalog-auditor",
    status: "draft_ready",
    revision: 1,
    messages: [],
    active_clarification: null,
    progress: [],
    files: [],
    draft_checksum: "b".repeat(64),
    validation: null,
    error_code: null,
    error_message: null,
    created_skill_id: null,
    session_kind: "revise",
    target_skill_id: "55555555-5555-4555-8555-555555555555",
    base_version_id: "66666666-6666-4666-8666-666666666666",
    base_version_number: 3,
    base_payload_checksum: "b".repeat(64),
    target_skill_deleted: false,
    base_files: [],
    created_at: NOW,
    updated_at: NOW,
  };
}

beforeEach(() => {
  rs.clearAllMocks();
  mockedUseQuery.mockReturnValue({ data: undefined } as never);
  mockedUsePrivateWorkAccess.mockReturnValue({
    apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
    scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
    runAbortable: (operation: (signal: AbortSignal) => Promise<unknown>) =>
      operation(new AbortController().signal),
    isActive: () => true,
  } as unknown as PrivateWorkAccess);
});

describe("useCreateSkillBuilderRevisionSession", () => {
  test("writes the revise session into the session cache", () => {
    const setQueryData = rs.fn();
    const invalidateQueries = rs.fn(async () => undefined);
    const mutations: MutationOptions[] = [];
    mockedUseQueryClient.mockReturnValue({
      setQueryData,
      invalidateQueries,
    } as never);
    mockedUseMutation.mockImplementation((options) => {
      mutations.push(options as unknown as MutationOptions);
      return { isPending: false, mutate: rs.fn() } as never;
    });

    useCreateSkillBuilderRevisionSession(ACCOUNT_ID, PROJECT_ID);

    const created = reviseSession();
    mutations[0]?.onSuccess({ data: created });

    expect(mutations[0]?.mutationKey).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "skill-builder",
      "mutation",
      "create-revision-session",
    ]);
    expect(setQueryData).toHaveBeenCalledWith(
      skillBuilderSessionKey(ACCOUNT_ID, PROJECT_ID, SESSION_ID),
      created,
    );
    expect(invalidateQueries).toHaveBeenCalledWith(
      skillBuilderSessionsInvalidation(ACCOUNT_ID, PROJECT_ID),
    );
  });
});
