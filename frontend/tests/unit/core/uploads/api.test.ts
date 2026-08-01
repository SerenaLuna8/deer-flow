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

function uploadLimits(overrides: Record<string, unknown> = {}) {
  return {
    max_files: 10,
    max_file_size: 100,
    max_total_size: 200,
    project_storage: {
      policy: "project_quota",
      remaining_bytes: 800,
    },
    request_id: "upload-limits",
    ...overrides,
  };
}

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

function pagedPrivateFile(index: number) {
  const suffix = String(index).padStart(12, "0");
  const displayName = `page-${String(index).padStart(3, "0")}.txt`;
  return {
    ...privateFile(displayName),
    id: `00000000-0000-4000-8000-${suffix}`,
  };
}

describe("project upload adapter", () => {
  test("uploads one multipart file at a time and aggregates mapped files", async () => {
    fetchWithAuth
      .mockResolvedValueOnce({
        ok: true,
        json: async () => uploadLimits(),
      })
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

    expect(fetchWithAuth).toHaveBeenCalledTimes(3);
    expect(fetchWithAuth.mock.calls[0]![0]).toBe(
      `${projectOptions.apiBaseURL}/threads/${threadId}/uploads/limits`,
    );
    for (const call of fetchWithAuth.mock.calls.slice(1)) {
      const body = call[1]?.body as FormData;
      expect(body.getAll("file")).toHaveLength(1);
      expect(body.getAll("files")).toHaveLength(0);
    }
    expect(result.files).toHaveLength(2);
    expect(result.files[0]).toMatchObject({
      id: fileId,
      kind: "upload",
      filename: "a.txt",
      virtual_path: "/mnt/user-data/uploads/a.txt",
    });
  });

  test("rejects a malformed POST upload response before it can be submitted", async () => {
    fetchWithAuth
      .mockResolvedValueOnce({
        ok: true,
        json: async () => uploadLimits(),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ...privateFile("a.txt"),
          id: "not-an-opaque-uuid",
        }),
      });
    const { uploadFiles } = await import("@/core/uploads/api");

    await expect(
      uploadFiles(
        threadId,
        [new File(["aaaa"], "a.txt", { type: "text/plain" })],
        projectOptions,
      ),
    ).rejects.toThrow();
    expect(fetchWithAuth).toHaveBeenCalledTimes(2);
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
    const { canDeleteProjectFile, deleteUploadedFile, listUploadedFiles } =
      await import("@/core/uploads/api");
    const listSignal = new AbortController().signal;
    const deleteSignal = new AbortController().signal;

    await expect(
      listUploadedFiles(threadId, projectOptions, listSignal),
    ).resolves.toMatchObject({
      count: 1,
      files: [{ id: fileId, kind: "upload", filename: "notes.txt" }],
    });
    expect(canDeleteProjectFile(true, "upload")).toBe(true);
    expect(canDeleteProjectFile(true, "workspace")).toBe(true);
    expect(canDeleteProjectFile(true, "output")).toBe(true);
    expect(canDeleteProjectFile(false, "upload")).toBe(false);
    expect(canDeleteProjectFile(true, undefined)).toBe(false);
    await deleteUploadedFile(threadId, fileId, projectOptions, deleteSignal);

    expect(fetchWithAuth.mock.calls[0]![0]).toBe(
      `${projectOptions.apiBaseURL}/threads/${threadId}/uploads?limit=100&offset=0`,
    );
    expect(fetchWithAuth.mock.calls[0]![1]).toEqual({ signal: listSignal });
    expect(fetchWithAuth.mock.calls[1]![0]).toBe(
      `${projectOptions.apiBaseURL}/threads/${threadId}/uploads?file_id=${fileId}`,
    );
    expect(fetchWithAuth.mock.calls[1]![1]).toMatchObject({
      method: "DELETE",
      signal: deleteSignal,
    });
  });

  test("pages the complete ready-file catalog without omissions", async () => {
    fetchWithAuth
      .mockResolvedValueOnce({
        ok: true,
        headers: new Headers({ "x-next-offset": "100" }),
        json: async () =>
          Array.from({ length: 100 }, (_, index) => pagedPrivateFile(index)),
      })
      .mockResolvedValueOnce({
        ok: true,
        headers: new Headers(),
        json: async () => [pagedPrivateFile(100)],
      });
    const { listUploadedFiles } = await import("@/core/uploads/api");
    const signal = new AbortController().signal;

    const result = await listUploadedFiles(threadId, projectOptions, signal);

    expect(result.count).toBe(101);
    expect(new Set(result.files.map((file) => file.id)).size).toBe(101);
    expect(result.files.at(0)?.filename).toBe("page-000.txt");
    expect(result.files.at(-1)?.filename).toBe("page-100.txt");
    expect(fetchWithAuth).toHaveBeenCalledTimes(2);
    expect(fetchWithAuth.mock.calls[0]).toEqual([
      `${projectOptions.apiBaseURL}/threads/${threadId}/uploads?limit=100&offset=0`,
      { signal },
    ]);
    expect(fetchWithAuth.mock.calls[1]).toEqual([
      `${projectOptions.apiBaseURL}/threads/${threadId}/uploads?limit=100&offset=100`,
      { signal },
    ]);
  });

  test("fails closed when a full upload page does not advance", async () => {
    const page = Array.from({ length: 100 }, (_, index) =>
      pagedPrivateFile(index),
    );
    fetchWithAuth
      .mockResolvedValueOnce({
        ok: true,
        headers: new Headers({ "x-next-offset": "100" }),
        json: async () => page,
      })
      .mockResolvedValueOnce({
        ok: true,
        headers: new Headers({ "x-next-offset": "200" }),
        json: async () => page,
      });
    const { listUploadedFiles } = await import("@/core/uploads/api");

    await expect(
      listUploadedFiles(threadId, projectOptions),
    ).rejects.toThrow("Uploaded file pagination did not advance");
  });

  test("does not start list or delete after the project scope is aborted", async () => {
    const { deleteUploadedFile, listUploadedFiles } =
      await import("@/core/uploads/api");
    const controller = new AbortController();
    controller.abort();

    await expect(
      listUploadedFiles(threadId, projectOptions, controller.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
    await expect(
      deleteUploadedFile(threadId, fileId, projectOptions, controller.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchWithAuth).not.toHaveBeenCalled();
  });

  test("loads and strictly parses exact project-private upload limits", async () => {
    fetchWithAuth.mockResolvedValueOnce({
      ok: true,
      json: async () => uploadLimits(),
    });
    const { getUploadLimits, supportsUploadLimits } =
      await import("@/core/uploads/api");
    const signal = new AbortController().signal;

    expect(supportsUploadLimits(projectOptions)).toBe(true);
    await expect(
      getUploadLimits(threadId, projectOptions, signal),
    ).resolves.toEqual(uploadLimits());
    expect(fetchWithAuth).toHaveBeenCalledWith(
      `${projectOptions.apiBaseURL}/threads/${threadId}/uploads/limits`,
      { signal },
    );
    expect(
      supportsUploadLimits({
        ...projectOptions,
        apiBaseURL:
          "http://localhost:2026/api/projects/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/private-work",
      }),
    ).toBe(false);
  });

  test("rejects malformed limit responses instead of trusting extra fields", async () => {
    fetchWithAuth.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ ...uploadLimits(), unexpected: "private" }),
    });
    const { getUploadLimits } = await import("@/core/uploads/api");

    await expect(getUploadLimits(threadId, projectOptions)).rejects.toThrow();
  });

  test("rejects project quota internals removed from the public response", async () => {
    fetchWithAuth.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        ...uploadLimits(),
        project_storage: {
          policy: "project_quota",
          remaining_bytes: 800,
          limit_bytes: 1_000,
          used_bytes: 100,
          reserved_bytes: 100,
        },
      }),
    });
    const { getUploadLimits } = await import("@/core/uploads/api");

    await expect(getUploadLimits(threadId, projectOptions)).rejects.toThrow();
  });

  test("prevalidates the complete batch before the first sequential POST", async () => {
    fetchWithAuth.mockResolvedValueOnce({
      ok: true,
      json: async () => uploadLimits({ max_total_size: 7 }),
    });
    const { uploadFiles } = await import("@/core/uploads/api");
    const { UploadLimitValidationError } =
      await import("@/core/uploads/errors");
    const files = [new File(["1234"], "a.txt"), new File(["5678"], "b.txt")];

    let caught: unknown;
    try {
      await uploadFiles(threadId, files, projectOptions);
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(UploadLimitValidationError);
    expect(
      (caught as InstanceType<typeof UploadLimitValidationError>).violations,
    ).toMatchObject([{ code: "max_total_size", limit: 7 }]);
    expect(fetchWithAuth).toHaveBeenCalledTimes(1);
  });

  test("preserves authoritative server 413 and 429 errors as structured data", async () => {
    const { uploadFiles } = await import("@/core/uploads/api");
    const { UploadApiError } = await import("@/core/uploads/errors");

    for (const scenario of [
      { status: 413, code: "PRIVATE_WORK_TOO_LARGE" },
      { status: 429, code: "PROJECT_STORAGE_QUOTA_EXCEEDED" },
    ]) {
      fetchWithAuth.mockReset();
      fetchWithAuth
        .mockResolvedValueOnce({
          ok: true,
          json: async () => uploadLimits(),
        })
        .mockResolvedValueOnce({
          ok: false,
          status: scenario.status,
          headers: new Headers({ "Retry-After": "1" }),
          json: async () => ({
            detail: {
              code: scenario.code,
              message: "Public upload rejection.",
              request_id: `request-${scenario.status}`,
            },
          }),
        });

      let caught: unknown;
      try {
        await uploadFiles(
          threadId,
          [new File(["data"], "notes.txt")],
          projectOptions,
        );
      } catch (error) {
        caught = error;
      }
      expect(caught).toBeInstanceOf(UploadApiError);
      expect(caught).toMatchObject({
        status: scenario.status,
        code: scenario.code,
        requestId: `request-${scenario.status}`,
        retryAfter: "1",
      });
    }
  });

  test("stops before POST when scope cancellation happens during preflight", async () => {
    let resolvePreflight!: (value: unknown) => void;
    fetchWithAuth.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePreflight = resolve;
      }),
    );
    const { uploadFiles } = await import("@/core/uploads/api");
    const controller = new AbortController();
    const result = uploadFiles(
      threadId,
      [new File(["data"], "notes.txt")],
      projectOptions,
      controller.signal,
    );

    controller.abort();
    resolvePreflight({
      ok: true,
      json: async () => uploadLimits(),
    });

    await expect(result).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchWithAuth).toHaveBeenCalledTimes(1);
  });

  test("does not start the next POST when cancellation follows the first POST", async () => {
    let resolveFirstPost!: (value: unknown) => void;
    fetchWithAuth
      .mockResolvedValueOnce({
        ok: true,
        json: async () => uploadLimits(),
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveFirstPost = resolve;
        }),
      );
    const { uploadFiles } = await import("@/core/uploads/api");
    const controller = new AbortController();
    const result = uploadFiles(
      threadId,
      [new File(["aaaa"], "a.txt"), new File(["bbbb"], "b.txt")],
      projectOptions,
      controller.signal,
    );
    while (fetchWithAuth.mock.calls.length < 2) {
      await Promise.resolve();
    }

    controller.abort();
    resolveFirstPost({
      ok: true,
      json: async () => privateFile("a.txt"),
    });

    await expect(result).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchWithAuth).toHaveBeenCalledTimes(2);
  });
});
