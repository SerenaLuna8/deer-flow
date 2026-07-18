import { beforeEach, describe, expect, test, rs } from "@rstest/core";

const fetchWithAuth = rs.fn();

rs.mock("@/core/api/fetcher", () => ({ fetch: fetchWithAuth }));

beforeEach(() => {
  fetchWithAuth.mockReset();
});

const projectOptions = {
  apiBaseURL:
    "http://localhost:2026/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work",
  scope: {
    accountId: "11111111-1111-4111-8111-111111111111",
    projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  },
};
const threadId = "33333333-3333-4333-8333-333333333333";
const fileId = "55555555-5555-4555-8555-555555555555";

function privateFile(displayName: string) {
  return {
    id: fileId,
    logical_path: `uploads/${displayName}`,
    display_name: displayName,
    kind: "upload",
    media_type: "text/plain",
    size: 4,
    sha256: "abcd",
    status: "ready",
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T00:00:00Z",
  };
}

describe("project upload adapter", () => {
  test("uploads one multipart file at a time and aggregates mapped files", async () => {
    fetchWithAuth
      .mockResolvedValueOnce({
        ok: true,
        json: async () => privateFile("a.txt"),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => privateFile("b.txt"),
      });
    const { uploadFiles } = await import("@/core/uploads/api");
    const files = [
      new File(["aaaa"], "a.txt", { type: "text/plain" }),
      new File(["bbbb"], "b.txt", { type: "text/plain" }),
    ];

    const result = await uploadFiles(threadId, files, projectOptions);

    expect(fetchWithAuth).toHaveBeenCalledTimes(2);
    for (const call of fetchWithAuth.mock.calls) {
      const body = call[1]?.body as FormData;
      expect(body.getAll("file")).toHaveLength(1);
      expect(body.getAll("files")).toHaveLength(0);
    }
    expect(result.files).toHaveLength(2);
    expect(result.files[0]).toMatchObject({
      id: fileId,
      filename: "a.txt",
      virtual_path: "/mnt/user-data/uploads/a.txt",
    });
  });

  test("maps GET uploads and deletes by file_id query", async () => {
    fetchWithAuth
      .mockResolvedValueOnce({
        ok: true,
        json: async () => [privateFile("notes.txt")],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ success: true }),
      });
    const { deleteUploadedFile, listUploadedFiles } =
      await import("@/core/uploads/api");

    await expect(
      listUploadedFiles(threadId, projectOptions),
    ).resolves.toMatchObject({
      count: 1,
      files: [{ id: fileId, filename: "notes.txt" }],
    });
    await deleteUploadedFile(threadId, fileId, projectOptions);

    expect(fetchWithAuth.mock.calls[0]![0]).toBe(
      `${projectOptions.apiBaseURL}/threads/${threadId}/uploads`,
    );
    expect(fetchWithAuth.mock.calls[1]![0]).toBe(
      `${projectOptions.apiBaseURL}/threads/${threadId}/uploads?file_id=${fileId}`,
    );
    expect(fetchWithAuth.mock.calls[1]![1]).toMatchObject({ method: "DELETE" });
  });

  test("declares project upload limits unavailable without requesting a route", async () => {
    const { supportsUploadLimits } = await import("@/core/uploads/api");
    expect(supportsUploadLimits(projectOptions)).toBe(false);
    expect(fetchWithAuth).not.toHaveBeenCalled();
  });
});
