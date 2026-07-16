import { beforeEach, describe, expect, test, rs } from "@rstest/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  AutomationApiError,
  createAutomationIdempotencyKey,
  triggerAutomation,
} from "@/core/project-automations/api";
import { useTriggerProjectAutomation } from "@/core/project-automations/hooks";

import { AUTOMATION_RUN } from "./fixtures";

rs.mock("react", () => ({
  useEffect: rs.fn(),
  useRef: rs.fn((initial) => ({ current: initial })),
}));
rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn((options) => options),
  useQuery: rs.fn((options) => options),
  useQueryClient: rs.fn(),
}));
rs.mock("@/core/private-work/provider", () => ({
  usePrivateWorkAccess: rs.fn(),
}));
rs.mock("@/core/project-automations/api", () => ({
  AutomationApiError: class AutomationApiError extends Error {
    constructor(
      readonly status: number,
      readonly code: string,
      message: string,
    ) {
      super(message);
    }
  },
  createAutomation: rs.fn(),
  createAutomationIdempotencyKey: rs.fn(),
  deleteAutomation: rs.fn(),
  getAutomation: rs.fn(),
  listAutomationRuns: rs.fn(),
  listAutomations: rs.fn(),
  listThreadAutomations: rs.fn(),
  pauseAutomation: rs.fn(),
  resumeAutomation: rs.fn(),
  triggerAutomation: rs.fn(),
  updateAutomation: rs.fn(),
}));

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const KEY_ONE = "55555555-5555-4555-8555-555555555555";
const KEY_TWO = "66666666-6666-4666-8666-666666666666";
const queryClient = { invalidateQueries: rs.fn(async () => undefined) };

type TriggerMutationOptions = {
  mutationFn: (taskId: string) => Promise<unknown>;
  mutationKey: readonly unknown[];
};

beforeEach(() => {
  rs.clearAllMocks();
  rs.mocked(useQueryClient).mockReturnValue(queryClient as never);
  rs.mocked(usePrivateWorkAccess).mockReturnValue({
    scope: SCOPE,
    client: {} as never,
    apiBaseURL: `/api/projects/${SCOPE.projectId}/private-work`,
    queryKeyPrefix: [],
    reconnectOnMount: true,
  });
  rs.mocked(useRef).mockImplementation((initial) => ({ current: initial }));
  rs.mocked(createAutomationIdempotencyKey)
    .mockReset()
    .mockReturnValueOnce(KEY_ONE)
    .mockReturnValueOnce(KEY_TWO);
});

describe("project automation trigger hook", () => {
  test("reuses one generated key across ambiguous retries without exposing it in options", async () => {
    const ambiguous = new AutomationApiError(
      0,
      "AUTOMATION_NETWORK_ERROR",
      "Automation service is unavailable",
    );
    rs.mocked(triggerAutomation).mockRejectedValue(ambiguous);

    const mutation =
      useTriggerProjectAutomation() as unknown as TriggerMutationOptions;
    await expect(mutation.mutationFn("task-1")).rejects.toBe(ambiguous);
    await expect(mutation.mutationFn("task-1")).rejects.toBe(ambiguous);

    expect(
      rs.mocked(triggerAutomation).mock.calls.map((call) => call[2]),
    ).toEqual([KEY_ONE, KEY_ONE]);
    expect(createAutomationIdempotencyKey).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(mutation)).not.toContain(KEY_ONE);
  });

  test("scope cleanup clears the retained key before the next action", async () => {
    const ambiguous = new AutomationApiError(
      0,
      "AUTOMATION_NETWORK_ERROR",
      "Automation service is unavailable",
    );
    rs.mocked(triggerAutomation)
      .mockRejectedValueOnce(ambiguous)
      .mockResolvedValueOnce(AUTOMATION_RUN);

    const mutation =
      useTriggerProjectAutomation() as unknown as TriggerMutationOptions;
    await expect(mutation.mutationFn("task-1")).rejects.toBe(ambiguous);
    const effect = rs.mocked(useEffect).mock.calls[0]?.[0];
    expect(effect).toBeDefined();
    effect?.()?.();
    await expect(mutation.mutationFn("task-1")).resolves.toEqual(
      AUTOMATION_RUN,
    );

    expect(
      rs.mocked(triggerAutomation).mock.calls.map((call) => call[2]),
    ).toEqual([KEY_ONE, KEY_TWO]);
  });
});
