import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import { useQuery } from "@tanstack/react-query";

rs.mock("react", () => ({
  useEffect: (effect: () => void) => effect(),
  useRef: <T>(initialValue: T) => ({ current: initialValue }),
  useState: <T>(initialValue: T) => [initialValue, rs.fn()] as const,
}));
rs.mock("@tanstack/react-query", () => ({
  useQuery: rs.fn(),
}));

import {
  EXECUTION_APPROVAL_POLL_INTERVAL_MS,
  executionApprovalByIdRefetchInterval,
  resolveObservedExecutionApprovalAnchor,
  shouldTrackPersistedExecutionApproval,
  useThreadExecutionApproval,
} from "@/core/execution-approvals/hooks";
import {
  executionApprovalProjectionSchema,
  type ExecutionApprovalProjection,
} from "@/core/execution-approvals/schemas";
import type { PrivateWorkAccess } from "@/core/private-work/types";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const APPROVAL_ID = "33333333-3333-4333-8333-333333333333";
const NOW = "2026-08-14T16:15:00Z";

const privateWork = {
  apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
  scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
} as PrivateWorkAccess;

const common = {
  approval_id: APPROVAL_ID,
  source_run_id: "run-1",
  source_tool_call_id: "call-1",
  execution_domain: {
    label: "Local Provider host",
    effective_user_label: "local OS user",
  },
  command_preview: "python count.py",
  cwd_preview: "/mnt/user-data/workspace",
  timeout_seconds: 60,
  source_agent: {
    kind: "lead",
    label: "Project Assistant",
    path: ["lead"],
  },
  risk_level: "host_execution",
  warning_code: "LOCAL_PROCESS_RUNS_ON_HOST",
  can_decide: false,
  continuation_run: null,
} as const;

const terminal = executionApprovalProjectionSchema.parse({
  ...common,
  status: "finished",
  version: "3",
  finished_at: NOW,
  exit_code: 0,
  result_summary_code: "PROCESS_EXITED",
});

const pending = executionApprovalProjectionSchema.parse({
  ...common,
  status: "pending",
  version: "1",
  can_decide: true,
  decision_expires_at: "2026-08-14T16:20:00Z",
  remaining_ttl_seconds: 300,
});

const deniedDeliveryPending = executionApprovalProjectionSchema.parse({
  ...common,
  status: "denied",
  version: "2",
  decision_at: NOW,
  denial_delivery_status: "pending",
});

const mockedUseQuery = rs.mocked(useQuery);

describe("useThreadExecutionApproval", () => {
  beforeEach(() => {
    rs.clearAllMocks();
  });

  test("polls missing and active projections but stops for terminal and error states", () => {
    const response = (approval: ExecutionApprovalProjection | null) => ({
      schema_version: 1 as const,
      server_time: NOW,
      approval,
    });

    expect(
      executionApprovalByIdRefetchInterval({
        state: { status: "success", data: response(null) },
      }),
    ).toBe(EXECUTION_APPROVAL_POLL_INTERVAL_MS);
    expect(
      executionApprovalByIdRefetchInterval({
        state: { status: "success", data: response(pending) },
      }),
    ).toBe(EXECUTION_APPROVAL_POLL_INTERVAL_MS);
    expect(
      executionApprovalByIdRefetchInterval({
        state: {
          status: "success",
          data: response(deniedDeliveryPending),
        },
      }),
    ).toBe(EXECUTION_APPROVAL_POLL_INTERVAL_MS);
    expect(
      executionApprovalByIdRefetchInterval({
        state: { status: "success", data: response(terminal) },
      }),
    ).toBe(false);
    expect(
      executionApprovalByIdRefetchInterval({
        state: { status: "error", data: response(pending) },
      }),
    ).toBe(false);
  });

  test("recovers a terminal projection by persisted ToolMessage id after active becomes null", () => {
    mockedUseQuery
      .mockReturnValueOnce({
        data: { schema_version: 1, server_time: NOW, approval: null },
      } as never)
      .mockReturnValueOnce({
        data: { schema_version: 1, server_time: NOW, approval: terminal },
      } as never);

    const result = useThreadExecutionApproval({
      privateWork,
      threadId: "thread-1",
      persistedApprovalId: APPROVAL_ID,
    });

    expect(result.observedApprovalId).toBe(APPROVAL_ID);
    expect(result.approval?.status).toBe("finished");
    expect(result.isPreparing).toBe(false);
    expect(mockedUseQuery.mock.calls[1]?.[0].queryKey).toContain(APPROVAL_ID);
  });

  test.each([
    {
      label: "is still loading",
      byId: { data: undefined, isPending: true, isSuccess: false },
    },
    {
      label: "temporarily returns a null projection",
      byId: {
        data: { schema_version: 1, server_time: NOW, approval: null },
        isPending: false,
        isSuccess: true,
      },
    },
  ])("keeps a persisted approval preparing while by-id $label", ({ byId }) => {
    mockedUseQuery
      .mockReturnValueOnce({
        data: { schema_version: 1, server_time: NOW, approval: null },
        isPending: false,
        isSuccess: true,
      } as never)
      .mockReturnValueOnce(byId as never);

    const result = useThreadExecutionApproval({
      privateWork,
      threadId: "thread-1",
      persistedApprovalId: APPROVAL_ID,
    });

    expect(result.observedApprovalId).toBe(APPROVAL_ID);
    expect(result.approval).toBeNull();
    expect(result.isPreparing).toBe(true);
  });

  test("retains the first active id while active briefly becomes null", () => {
    const observed = resolveObservedExecutionApprovalAnchor({
      activeApprovalId: APPROVAL_ID,
      current: null,
      persistedApprovalId: null,
      threadId: "thread-1",
    });

    expect(
      resolveObservedExecutionApprovalAnchor({
        activeApprovalId: null,
        current: observed,
        persistedApprovalId: null,
        threadId: "thread-1",
      }),
    ).toBe(observed);
  });

  test("advances to a newly persisted serial approval after active becomes null", () => {
    const current = { threadId: "thread-1", approvalId: APPROVAL_ID };
    const nextApprovalId = "44444444-4444-4444-8444-444444444444";

    expect(
      resolveObservedExecutionApprovalAnchor({
        activeApprovalId: null,
        current,
        persistedApprovalId: nextApprovalId,
        persistedApprovalChanged: false,
        threadId: "thread-1",
      }),
    ).toBe(current);
    expect(
      resolveObservedExecutionApprovalAnchor({
        activeApprovalId: null,
        current,
        persistedApprovalId: nextApprovalId,
        persistedApprovalChanged: true,
        threadId: "thread-1",
      }),
    ).toEqual({ threadId: "thread-1", approvalId: nextApprovalId });
  });

  test("does not consume a newer persisted id behind a stale active approval", () => {
    const nextApprovalId = "44444444-4444-4444-8444-444444444444";
    let lastPersistedApprovalId: string | null = APPROVAL_ID;

    if (
      shouldTrackPersistedExecutionApproval({
        activeApprovalId: APPROVAL_ID,
        persistedApprovalId: nextApprovalId,
      })
    ) {
      lastPersistedApprovalId = nextApprovalId;
    }
    expect(lastPersistedApprovalId).toBe(APPROVAL_ID);

    const persistedApprovalChanged = lastPersistedApprovalId !== nextApprovalId;
    expect(
      resolveObservedExecutionApprovalAnchor({
        activeApprovalId: null,
        current: { threadId: "thread-1", approvalId: APPROVAL_ID },
        persistedApprovalId: nextApprovalId,
        persistedApprovalChanged,
        threadId: "thread-1",
      }),
    ).toEqual({ threadId: "thread-1", approvalId: nextApprovalId });
  });

  test("does not carry an observed id into another thread", () => {
    expect(
      resolveObservedExecutionApprovalAnchor({
        activeApprovalId: null,
        current: { threadId: "thread-1", approvalId: APPROVAL_ID },
        persistedApprovalId: null,
        threadId: "thread-2",
      }),
    ).toBeNull();
  });

  test("does not regress from a newer active projection to stale by-id data", () => {
    const pending = executionApprovalProjectionSchema.parse({
      ...common,
      status: "pending",
      version: "1",
      can_decide: true,
      decision_expires_at: "2026-08-14T16:20:00Z",
      remaining_ttl_seconds: 300,
    });
    const approved = executionApprovalProjectionSchema.parse({
      ...common,
      status: "approved",
      version: "2",
      decision_at: NOW,
      claim_expires_at: "2026-08-14T16:16:00Z",
    });
    mockedUseQuery
      .mockReturnValueOnce({
        data: { schema_version: 1, server_time: NOW, approval: approved },
      } as never)
      .mockReturnValueOnce({
        data: { schema_version: 1, server_time: NOW, approval: pending },
      } as never);

    const result = useThreadExecutionApproval({
      privateWork,
      threadId: "thread-1",
    });

    expect(result.approval?.status).toBe("approved");
    expect(result.approval?.continuation_run).toBeNull();
  });
});
