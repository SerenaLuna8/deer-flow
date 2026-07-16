import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  AutomationApiError,
  createAutomation,
  createAutomationIdempotencyKey,
  deleteAutomation,
  getAutomation,
  listAutomationRuns,
  listAutomations,
  listThreadAutomations,
  pauseAutomation,
  resumeAutomation,
  triggerAutomation,
  updateAutomation,
} from "@/core/project-automations/api";

import { AUTOMATION, AUTOMATION_RUN } from "./fixtures";

const mockedFetch = rs.mocked(fetchWithAuth);
const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const IDEMPOTENCY_KEY = "55555555-5555-4555-8555-555555555555";
const CREATE_INPUT = {
  title: AUTOMATION.title,
  prompt: AUTOMATION.prompt,
  context_mode: AUTOMATION.context_mode,
  thread_id: AUTOMATION.thread_id,
  agent_asset_id: AUTOMATION.agent_asset_id,
  agent_scope: AUTOMATION.agent_scope,
  schedule_type: AUTOMATION.schedule_type,
  schedule_spec: AUTOMATION.schedule_spec,
  timezone: AUTOMATION.timezone,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("project automation API", () => {
  test("uses only strict project URLs for every read endpoint", async () => {
    mockedFetch
      .mockResolvedValueOnce(jsonResponse({ items: [AUTOMATION] }))
      .mockResolvedValueOnce(jsonResponse({ items: [AUTOMATION] }))
      .mockResolvedValueOnce(jsonResponse(AUTOMATION))
      .mockResolvedValueOnce(jsonResponse({ items: [AUTOMATION_RUN] }));
    const signal = new AbortController().signal;

    await listAutomations(SCOPE, { limit: 20, offset: 10 }, signal);
    await listThreadAutomations(
      SCOPE,
      "33333333-3333-4333-8333-333333333333",
      { limit: 5 },
      signal,
    );
    await getAutomation(SCOPE, "task/1", signal);
    await listAutomationRuns(SCOPE, "task/1", { offset: 10 }, signal);

    expect(mockedFetch.mock.calls).toEqual([
      [
        `/backend/api/projects/${SCOPE.projectId}/automations?limit=20&offset=10`,
        { signal },
      ],
      [
        `/backend/api/projects/${SCOPE.projectId}/automations/threads/33333333-3333-4333-8333-333333333333?limit=5&offset=0`,
        { signal },
      ],
      [
        `/backend/api/projects/${SCOPE.projectId}/automations/task%2F1`,
        { signal },
      ],
      [
        `/backend/api/projects/${SCOPE.projectId}/automations/task%2F1/runs?limit=50&offset=10`,
        { signal },
      ],
    ]);
  });

  test("sends only whitelisted optimistic mutation bodies", async () => {
    mockedFetch
      .mockResolvedValueOnce(jsonResponse(AUTOMATION, 201))
      .mockResolvedValueOnce(
        jsonResponse({ ...AUTOMATION, title: "Updated", version: 2 }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ ...AUTOMATION, status: "paused", version: 2 }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ ...AUTOMATION, status: "enabled", version: 3 }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ id: AUTOMATION.id, deleted: true }),
      );

    await createAutomation(SCOPE, CREATE_INPUT);
    await updateAutomation(SCOPE, AUTOMATION.id, {
      expected_version: 1,
      title: "Updated",
    });
    await pauseAutomation(SCOPE, AUTOMATION.id, 1);
    await resumeAutomation(SCOPE, AUTOMATION.id, 2);
    await deleteAutomation(SCOPE, AUTOMATION.id, 3);

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/backend/api/projects/${SCOPE.projectId}/automations`,
      `/backend/api/projects/${SCOPE.projectId}/automations/${AUTOMATION.id}`,
      `/backend/api/projects/${SCOPE.projectId}/automations/${AUTOMATION.id}/pause`,
      `/backend/api/projects/${SCOPE.projectId}/automations/${AUTOMATION.id}/resume`,
      `/backend/api/projects/${SCOPE.projectId}/automations/${AUTOMATION.id}`,
    ]);
    expect(mockedFetch.mock.calls.map(([, init]) => init?.body)).toEqual([
      JSON.stringify(CREATE_INPUT),
      JSON.stringify({ expected_version: 1, title: "Updated" }),
      JSON.stringify({ expected_version: 1 }),
      JSON.stringify({ expected_version: 2 }),
      JSON.stringify({ expected_version: 3 }),
    ]);
  });

  test("sends the manual idempotency key only as a header", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(AUTOMATION_RUN));

    await triggerAutomation(SCOPE, "task/1", IDEMPOTENCY_KEY);

    const [url, init] = mockedFetch.mock.calls[0]!;
    expect(url).toBe(
      `/backend/api/projects/${SCOPE.projectId}/automations/task%2F1/trigger`,
    );
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBe(
      IDEMPOTENCY_KEY,
    );
    expect(init?.body).toBeUndefined();
  });

  test("generates and validates manual idempotency keys without transport", () => {
    expect(createAutomationIdempotencyKey(() => IDEMPOTENCY_KEY)).toBe(
      IDEMPOTENCY_KEY,
    );
    expect(() => createAutomationIdempotencyKey(() => "not-a-uuid")).toThrow(
      AutomationApiError,
    );
    expect(mockedFetch).not.toHaveBeenCalled();
  });

  test("validates scope and bodies before transport and rejects internal responses", async () => {
    await expect(
      createAutomation(SCOPE, {
        ...CREATE_INPUT,
        owner_user_id: SCOPE.accountId,
      } as never),
    ).rejects.toMatchObject({ code: "AUTOMATION_VALIDATION_FAILED" });
    await expect(
      listAutomations({ ...SCOPE, projectId: "not-a-project" }),
    ).rejects.toMatchObject({ code: "AUTOMATION_VALIDATION_FAILED" });
    expect(mockedFetch).not.toHaveBeenCalled();

    mockedFetch.mockResolvedValueOnce(
      jsonResponse({ ...AUTOMATION, lease_owner: "private-worker" }),
    );
    await expect(getAutomation(SCOPE, AUTOMATION.id)).rejects.toMatchObject({
      code: "AUTOMATION_RESPONSE_INVALID",
      message: "Automation response was invalid",
    });
  });

  test("maps only approved public errors and preserves auth and abort semantics", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            code: "AUTOMATION_FORBIDDEN",
            message: "database row owner secret",
            request_id: "req-forbidden",
          },
        },
        403,
      ),
    );
    const forbidden = await listAutomations(SCOPE).catch(
      (error: unknown) => error,
    );
    expect(forbidden).toBeInstanceOf(AutomationApiError);
    expect(forbidden).toMatchObject({
      status: 403,
      code: "AUTOMATION_FORBIDDEN",
      message: "Automation action is forbidden.",
    });

    mockedFetch.mockRejectedValueOnce(new AuthRequiredError());
    await expect(listAutomations(SCOPE)).rejects.toMatchObject({
      status: 401,
      code: "AUTH_REQUIRED",
      message: "Authentication required",
    });

    const aborted = new DOMException("Aborted", "AbortError");
    mockedFetch.mockRejectedValueOnce(aborted);
    await expect(listAutomations(SCOPE)).rejects.toBe(aborted);
  });
});
