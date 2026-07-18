import { beforeEach, expect, rs, test } from "@rstest/core";

const fetchWithAuth = rs.fn();

rs.mock("@/core/api/fetcher", () => ({
  fetch: fetchWithAuth,
}));

const privateWork = {
  apiBaseURL: "/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work",
};

beforeEach(() => {
  fetchWithAuth.mockReset();
});

test("feedback mutations use only the project private-work route", async () => {
  fetchWithAuth
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        feedback_id: "feedback-1",
        rating: 1,
        comment: null,
      }),
    })
    .mockResolvedValueOnce({ ok: true });

  const { deleteFeedback, upsertFeedback } =
    await import("@/core/api/feedback");
  await upsertFeedback(privateWork, "thread/1", "run/1", 1);
  await deleteFeedback(privateWork, "thread/1", "run/1");

  const url = `${privateWork.apiBaseURL}/threads/thread%2F1/runs/run%2F1/feedback`;
  expect(fetchWithAuth).toHaveBeenNthCalledWith(1, url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rating: 1, comment: null }),
  });
  expect(fetchWithAuth).toHaveBeenNthCalledWith(2, url, {
    method: "DELETE",
  });
});
