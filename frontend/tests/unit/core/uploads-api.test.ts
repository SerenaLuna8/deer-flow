import { beforeEach, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { uploadFiles } from "@/core/uploads/api";

const mockedFetch = rs.mocked(fetchWithAuth);

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

test("reports each completed upload before a later file fails", async () => {
  mockedFetch
    .mockResolvedValueOnce(
      jsonResponse(200, {
        max_files: 10,
        max_file_size: 1_000_000,
        max_total_size: 2_000_000,
        project_storage: {
          policy: "project_quota",
          remaining_bytes: 5_000_000,
        },
        request_id: "limits-1",
      }),
    )
    .mockResolvedValueOnce(
      jsonResponse(201, {
        id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
        logical_path: "uploads/a.png",
        display_name: "a.png",
        kind: "upload",
        media_type: "image/png",
        size: 1,
        sha256: "a".repeat(64),
        status: "ready",
        created_at: "2026-08-16T00:00:00Z",
        updated_at: "2026-08-16T00:00:00Z",
      }),
    )
    .mockResolvedValueOnce(
      jsonResponse(409, {
        detail: {
          code: "PRIVATE_WORK_CONFLICT",
          message: "Upload path already exists",
          request_id: "upload-2",
        },
      }),
    );

  const completed: string[] = [];
  await expect(
    uploadFiles(
      "11111111-1111-4111-8111-111111111111",
      [
        new File(["a"], "a.png", { type: "image/png" }),
        new File(["b"], "b.png", { type: "image/png" }),
      ],
      {
        apiBaseURL:
          "/api/projects/22222222-2222-4222-8222-222222222222/private-work",
        scope: {
          accountId: "33333333-3333-4333-8333-333333333333",
          projectId: "22222222-2222-4222-8222-222222222222",
        },
      },
      undefined,
      (uploaded) => completed.push(uploaded.id!),
    ),
  ).rejects.toThrow("Upload path already exists");

  expect(completed).toEqual(["8f31eef3-0662-42c5-809c-3bbbe2c663af"]);
  expect(mockedFetch).toHaveBeenCalledTimes(3);
});
