import { beforeEach, expect, rs, test } from "@rstest/core";

const fetchWithAuth = rs.fn();

rs.mock("@/core/api/fetcher", () => ({
  fetch: fetchWithAuth,
}));

const privateWork = {
  apiBaseURL: "/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work",
};
const response = {
  feedback_id: "feedback-1",
  thread_id: "thread/1",
  run_id: "run/1",
  message_id: "message-1",
  rating: 1 as const,
  comment: null,
  created_at: "2026-07-22T10:00:00+00:00",
};

beforeEach(() => {
  fetchWithAuth.mockReset();
});

test("feedback GET, PUT and DELETE stay on the project private-work route", async () => {
  fetchWithAuth
    .mockResolvedValueOnce({ ok: true, json: async () => null })
    .mockResolvedValueOnce({ ok: true, json: async () => response })
    .mockResolvedValueOnce({ ok: true, status: 204 });
  const queryAbort = new AbortController();
  const mutationAbort = new AbortController();

  const { deleteFeedback, getFeedback, upsertFeedback } =
    await import("@/core/api/feedback");
  expect(
    await getFeedback(privateWork, "thread/1", "run/1", queryAbort.signal),
  ).toBeNull();
  expect(
    await upsertFeedback(
      privateWork,
      "thread/1",
      "run/1",
      1,
      "message-1",
      null,
      mutationAbort.signal,
    ),
  ).toEqual(response);
  await deleteFeedback(privateWork, "thread/1", "run/1", mutationAbort.signal);

  const url = `${privateWork.apiBaseURL}/threads/thread%2F1/runs/run%2F1/feedback`;
  expect(fetchWithAuth).toHaveBeenNthCalledWith(1, url, {
    signal: queryAbort.signal,
  });
  expect(fetchWithAuth).toHaveBeenNthCalledWith(2, url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      rating: 1,
      message_id: "message-1",
      comment: null,
    }),
    signal: mutationAbort.signal,
  });
  expect(fetchWithAuth).toHaveBeenNthCalledWith(3, url, {
    method: "DELETE",
    signal: mutationAbort.signal,
  });
});

test("feedback responses reject private scope coordinates", async () => {
  fetchWithAuth.mockResolvedValueOnce({
    ok: true,
    json: async () => ({
      ...response,
      project_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      owner_user_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }),
  });

  const { getFeedback } = await import("@/core/api/feedback");
  await expect(getFeedback(privateWork, "thread/1", "run/1")).rejects.toThrow();
});

test("feedback delete does not treat a hidden run as success", async () => {
  fetchWithAuth.mockResolvedValueOnce({
    ok: false,
    status: 404,
    json: async () => ({
      detail: {
        code: "PRIVATE_WORK_NOT_FOUND",
        message: "Private work was not found.",
      },
    }),
  });

  const { deleteFeedback } = await import("@/core/api/feedback");
  await expect(
    deleteFeedback(privateWork, "thread/1", "run/1"),
  ).rejects.toMatchObject({
    status: 404,
    code: "PRIVATE_WORK_NOT_FOUND",
  });
});
