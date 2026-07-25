import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, rs } from "@rstest/core";

import type { ProjectPrivateWorkScope } from "@/core/private-work/types";
import {
  deleteUploadedFile,
  getUploadLimits,
  listUploadedFiles,
  uploadFiles,
} from "@/core/uploads/api";
import {
  useDeleteUploadedFile,
  useUploadedFiles,
  useUploadFiles,
  useUploadLimits,
} from "@/core/uploads/hooks";

const invalidateQueries = rs.fn();

rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn((options) => options),
  useQuery: rs.fn((options) => options),
  useQueryClient: rs.fn(() => ({ invalidateQueries })),
}));
rs.mock("@/core/private-work/provider", () => ({
  usePrivateWorkAccess: rs.fn((explicit) => explicit),
}));
rs.mock("@/core/uploads/api", () => ({
  deleteUploadedFile: rs.fn(),
  getUploadLimits: rs.fn(),
  listUploadedFiles: rs.fn(),
  supportsUploadLimits: rs.fn(() => true),
  uploadFiles: rs.fn(),
}));

const threadId = "33333333-3333-4333-8333-333333333333";

function privateWork(
  accountId: string,
  projectId: string,
): ProjectPrivateWorkScope {
  return {
    scope: { accountId, projectId },
    apiBaseURL: `http://localhost:2026/api/projects/${projectId}/private-work`,
    client: {} as ProjectPrivateWorkScope["client"],
    queryKeyPrefix: [],
    reconnectOnMount: false,
  };
}

test("upload limit queries isolate account project and thread coordinates", async () => {
  const accountA = "11111111-1111-4111-8111-111111111111";
  const accountB = "22222222-2222-4222-8222-222222222222";
  const projectA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const projectB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const configA = useUploadLimits(
    threadId,
    privateWork(accountA, projectA),
  ) as unknown as {
    queryKey: readonly unknown[];
    queryFn: (context: { signal: AbortSignal }) => Promise<unknown>;
  };
  const configOtherAccount = useUploadLimits(
    threadId,
    privateWork(accountB, projectA),
  ) as unknown as { queryKey: readonly unknown[] };
  const configOtherProject = useUploadLimits(
    threadId,
    privateWork(accountA, projectB),
  ) as unknown as { queryKey: readonly unknown[] };

  expect(configA.queryKey).toEqual([
    "account",
    accountA,
    "project",
    projectA,
    "private-work",
    "uploads",
    "limits",
    threadId,
  ]);
  expect(configA.queryKey).not.toEqual(configOtherAccount.queryKey);
  expect(configA.queryKey).not.toEqual(configOtherProject.queryKey);

  const signal = new AbortController().signal;
  await configA.queryFn({ signal });
  expect(getUploadLimits).toHaveBeenCalledWith(
    threadId,
    expect.objectContaining({
      scope: { accountId: accountA, projectId: projectA },
    }),
    signal,
  );
});

test("upload mutation binds the complete batch to the active scope generation", async () => {
  const accountId = "11111111-1111-4111-8111-111111111111";
  const projectId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const signal = new AbortController().signal;
  const runAbortable = rs.fn(
    (operation: (currentSignal: AbortSignal) => Promise<unknown>) =>
      operation(signal),
  );
  const access = {
    ...privateWork(accountId, projectId),
    runAbortable,
    isActive: () => true,
  } as never;
  const mutation = useUploadFiles(threadId, access) as unknown as {
    mutationFn: (files: File[]) => Promise<unknown>;
    onSuccess: () => void;
  };
  const files = [new File(["data"], "notes.txt")];

  await mutation.mutationFn(files);

  expect(runAbortable).toHaveBeenCalledTimes(1);
  expect(uploadFiles).toHaveBeenCalledWith(
    threadId,
    files,
    expect.objectContaining({ scope: { accountId, projectId } }),
    signal,
  );
  mutation.onSuccess();
  expect(invalidateQueries).toHaveBeenCalledWith({
    queryKey: [
      "account",
      accountId,
      "project",
      projectId,
      "private-work",
      "uploads",
      "list",
      threadId,
    ],
  });
});

test("uploaded-file query forwards TanStack cancellation to the scoped request", async () => {
  const accountId = "11111111-1111-4111-8111-111111111111";
  const projectId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const access = privateWork(accountId, projectId);
  const query = useUploadedFiles(threadId, access) as unknown as {
    queryFn: (context: { signal: AbortSignal }) => Promise<unknown>;
  };
  const signal = new AbortController().signal;

  await query.queryFn({ signal });

  expect(listUploadedFiles).toHaveBeenCalledWith(
    threadId,
    expect.objectContaining({ scope: { accountId, projectId } }),
    signal,
  );
});

test("delete mutation aborts with its scope and ignores a late inactive callback", async () => {
  invalidateQueries.mockClear();
  const accountId = "11111111-1111-4111-8111-111111111111";
  const projectId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const signal = new AbortController().signal;
  let active = true;
  const runAbortable = rs.fn(
    (operation: (currentSignal: AbortSignal) => Promise<unknown>) =>
      operation(signal),
  );
  const access = {
    ...privateWork(accountId, projectId),
    runAbortable,
    isActive: () => active,
  } as never;
  const mutation = useDeleteUploadedFile(threadId, access) as unknown as {
    mutationFn: (fileId: string) => Promise<unknown>;
    onSuccess: () => void;
  };
  const fileId = "55555555-5555-4555-8555-555555555555";

  await mutation.mutationFn(fileId);

  expect(runAbortable).toHaveBeenCalledTimes(1);
  expect(deleteUploadedFile).toHaveBeenCalledWith(
    threadId,
    fileId,
    expect.objectContaining({ scope: { accountId, projectId } }),
    signal,
  );
  active = false;
  mutation.onSuccess();
  expect(invalidateQueries).not.toHaveBeenCalled();
});

test("thread submit wraps preflight and every sequential POST in one abortable scope", () => {
  const source = readFileSync(
    resolve(process.cwd(), "src/core/threads/hooks.ts"),
    "utf8",
  );

  expect(source).toContain(
    "const uploadResponse = await runPrivateWorkAbortable(",
  );
  expect(source).toContain("uploadFiles(threadId, files, privateWork, signal)");
});

test("an inactive project scope cannot invalidate the next project's cache", () => {
  invalidateQueries.mockClear();
  const access = {
    ...privateWork(
      "11111111-1111-4111-8111-111111111111",
      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    ),
    isActive: () => false,
  } as never;
  const mutation = useUploadFiles(threadId, access) as unknown as {
    onSuccess: () => void;
  };

  mutation.onSuccess();

  expect(invalidateQueries).not.toHaveBeenCalled();
});
