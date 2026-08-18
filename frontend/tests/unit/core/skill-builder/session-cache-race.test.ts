import { describe, expect, test } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import {
  skillBuilderSessionKey,
  updateSkillBuilderSessionCacheAfterMutation,
} from "@/core/skill-builder";
import type { SkillBuilderSession } from "@/core/skill-builder/types";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_ID = "33333333-3333-4333-8333-333333333333";
const RUN_ID = "44444444-4444-4444-8444-444444444444";
const NOW = "2026-08-14T08:00:00+08:00";

function session(): SkillBuilderSession {
  return {
    id: SESSION_ID,
    project_id: PROJECT_ID,
    owner_user_id: "owner-1",
    thread_id: "55555555-5555-4555-8555-555555555555",
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
    session_kind: "create",
    target_skill_id: null,
    base_version_id: null,
    base_version_number: null,
    base_payload_checksum: null,
    target_skill_deleted: false,
    base_files: [],
    created_at: NOW,
    updated_at: NOW,
  };
}

describe("Skill Builder exact-session cache", () => {
  test("an older in-flight GET cannot erase a same-revision Run admission", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const key = skillBuilderSessionKey(ACCOUNT_ID, PROJECT_ID, SESSION_ID);
    const stale = session();
    queryClient.setQueryData(key, stale);
    let resolveStale: ((value: SkillBuilderSession) => void) | undefined;
    const staleRead = queryClient
      .fetchQuery({
        queryKey: key,
        queryFn: () =>
          new Promise<SkillBuilderSession>((resolve) => {
            resolveStale = resolve;
          }),
      })
      .catch(() => undefined);

    await Promise.resolve();
    await updateSkillBuilderSessionCacheAfterMutation(
      queryClient,
      ACCOUNT_ID,
      PROJECT_ID,
      SESSION_ID,
      (current) =>
        current
          ? {
              ...current,
              activeRun: {
                runId: RUN_ID,
                status: "pending",
                streamUrl: `/api/runs/${RUN_ID}/stream`,
              },
            }
          : current,
    );

    resolveStale?.(stale);
    await staleRead;

    expect(queryClient.getQueryData<SkillBuilderSession>(key)).toMatchObject({
      revision: 1,
      activeRun: { runId: RUN_ID, status: "pending" },
    });
  });
});
