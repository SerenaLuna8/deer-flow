import { beforeEach, expect, test, rs } from "@rstest/core";

const fetchWithAuth = rs.fn();

rs.mock("@/core/api/fetcher", () => ({ fetch: fetchWithAuth }));

beforeEach(() => {
  fetchWithAuth.mockReset();
});

test("upload APIs accept the project private-work REST base", async () => {
  fetchWithAuth.mockResolvedValue({
    ok: true,
    json: async () => ({ success: true, files: [], skipped_files: [] }),
  });
  const apiBaseURL =
    "http://localhost:2026/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work";
  const { uploadFiles } = await import("@/core/uploads/api");

  await uploadFiles("thread/1", [], { apiBaseURL });

  expect(fetchWithAuth).toHaveBeenCalledWith(
    `${apiBaseURL}/threads/thread%2F1/uploads`,
    expect.objectContaining({ method: "POST" }),
  );
});
