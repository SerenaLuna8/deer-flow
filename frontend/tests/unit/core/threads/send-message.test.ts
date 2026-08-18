import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import {
  buildThreadSubmitCheckpointOptions,
  buildThreadSubmitMessages,
  uploadedFileInfoToMessage,
} from "@/core/threads/hooks";
import {
  admitRunAndNotify,
  createMessageSendAttempt,
  createRunAdmissionLatch,
  createUploadedAttachmentRefCache,
  forgetUploadedAttachmentRefs,
  isCurrentMessageSendAttempt,
  isRunAdmissionNotConfirmedError,
  isCurrentThreadCallback,
  monitorRunAdmissionLifecycle,
  planAttachmentUploadRetry,
  rememberUploadedAttachmentRef,
  retainUploadedAttachmentRefs,
  shouldIgnoreMetadataLessStreamError,
  shouldIgnoreAttributedThreadCallback,
} from "@/core/threads/send-message";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

test("omits the SDK branch checkpoint after a successful compaction", () => {
  expect(buildThreadSubmitCheckpointOptions(true)).toEqual({
    checkpoint: null,
  });
  expect(buildThreadSubmitCheckpointOptions(false)).toEqual({});
  expect(buildThreadSubmitCheckpointOptions(undefined)).toEqual({});
});

test("builds thread submit messages with hidden sidecar context before the visible user message", () => {
  const hiddenContext = {
    type: "human",
    content: "Hidden sidecar context",
    additional_kwargs: {
      hide_from_ui: true,
      sidecar_context: true,
    },
  } as Message;

  const messages = buildThreadSubmitMessages({
    text: "What should we do next?",
    messageId: "human-visible-1",
    additionalInputMessages: [hiddenContext],
  });

  expect(messages).toEqual([
    hiddenContext,
    {
      type: "human",
      id: "human-visible-1",
      content: [{ type: "text", text: "What should we do next?" }],
      additional_kwargs: {},
    },
  ]);
});

test("keeps uploaded files on the visible user message only", () => {
  const messages = buildThreadSubmitMessages({
    text: "Use this file",
    messageId: "human-visible-2",
    additionalInputMessages: [
      {
        type: "human",
        content: "Hidden sidecar context",
        additional_kwargs: { hide_from_ui: true },
      } as Message,
    ],
    filesForSubmit: [
      {
        file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
        filename: "report.pdf",
        size: 42,
        path: "/uploads/report.pdf",
        status: "uploaded",
      },
    ],
  });

  expect(messages[0]?.additional_kwargs).toEqual({ hide_from_ui: true });
  expect(messages[1]?.additional_kwargs).toEqual({
    files: [
      {
        file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
        filename: "report.pdf",
        size: 42,
        path: "/uploads/report.pdf",
        status: "uploaded",
      },
    ],
  });
});

test("maps a private upload response to current-run file authority metadata", () => {
  expect(
    uploadedFileInfoToMessage({
      id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      kind: "upload",
      filename: "report.pdf",
      size: 42,
      path: "/mnt/user-data/uploads/report.pdf",
      virtual_path: "/mnt/user-data/uploads/report.pdf",
      artifact_url: "/api/files/opaque",
    }),
  ).toEqual({
    file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
    filename: "report.pdf",
    size: 42,
    path: "/mnt/user-data/uploads/report.pdf",
    status: "uploaded",
  });
});

test("rejects an uploaded response without an opaque file id", () => {
  expect(() =>
    uploadedFileInfoToMessage({
      filename: "report.pdf",
      size: 42,
      path: "/mnt/user-data/uploads/report.pdf",
      virtual_path: "/mnt/user-data/uploads/report.pdf",
      artifact_url: "/api/files/opaque",
    }),
  ).toThrow("Uploaded file response is missing its opaque id");
});

test("rejects an uploaded response with a malformed opaque file id", () => {
  expect(() =>
    uploadedFileInfoToMessage({
      id: "not-an-opaque-uuid",
      filename: "report.pdf",
      size: 42,
      path: "/mnt/user-data/uploads/report.pdf",
      virtual_path: "/mnt/user-data/uploads/report.pdf",
      artifact_url: "/api/files/opaque",
    }),
  ).toThrow("Uploaded file response has an invalid opaque id");
});

test("keeps human input response metadata on the hidden user message", () => {
  const response = {
    version: 1,
    kind: "human_input_response",
    source: "ask_clarification",
    request_id: "clarification:call-abc",
    response_kind: "option",
    option_id: "option-2",
    value: "staging",
  };

  const messages = buildThreadSubmitMessages({
    text: 'For your clarification "Which environment?", my answer is: staging',
    messageId: "human-hidden-response",
    additionalKwargs: {
      hide_from_ui: true,
      human_input_response: response,
    },
  });

  expect(messages).toEqual([
    {
      type: "human",
      id: "human-hidden-response",
      content: [
        {
          type: "text",
          text: 'For your clarification "Which environment?", my answer is: staging',
        },
      ],
      additional_kwargs: {
        hide_from_ui: true,
        human_input_response: response,
      },
    },
  ]);
});

test("does not settle the composer before Run admission", async () => {
  const admission = createRunAdmissionLatch();
  let settled = false;
  void admission.promise.then(() => {
    settled = true;
  });

  await Promise.resolve();
  expect(settled).toBe(false);

  expect(admission.admit()).toBe(true);
  await admission.promise;
  expect(settled).toBe(true);
  expect(admission.admit()).toBe(false);
});

test("observer failures cannot veto an admitted Run", async () => {
  const admission = createRunAdmissionLatch();
  let secondObserverCalls = 0;

  expect(
    admitRunAndNotify(
      admission,
      () => {
        throw new Error("local draft cleanup failed");
      },
      () => {
        secondObserverCalls += 1;
      },
    ),
  ).toBe(true);

  await expect(admission.promise).resolves.toBeUndefined();
  expect(secondObserverCalls).toBe(1);
  expect(admission.isPending()).toBe(false);
});

test("rejects a retryable composer when submit fails before admission", async () => {
  const admission = createRunAdmissionLatch();
  const lifecycle = deferred<void>();
  let admissionFailures = 0;
  let lifecycleSettles = 0;
  const observed = monitorRunAdmissionLifecycle({
    admission,
    lifecycle: lifecycle.promise,
    onAdmissionFailure: () => {
      admissionFailures += 1;
    },
    onSettled: () => {
      lifecycleSettles += 1;
    },
  });

  lifecycle.reject(new Error("admission failed"));
  await expect(admission.promise).rejects.toThrow("admission failed");
  await observed;
  expect(admissionFailures).toBe(1);
  expect(lifecycleSettles).toBe(1);
});

test("consumes a terminal failure after admission without reopening the composer", async () => {
  const admission = createRunAdmissionLatch();
  const lifecycle = deferred<void>();
  let admissionFailures = 0;
  let lifecycleSettles = 0;
  const observed = monitorRunAdmissionLifecycle({
    admission,
    lifecycle: lifecycle.promise,
    onAdmissionFailure: () => {
      admissionFailures += 1;
    },
    onSettled: () => {
      lifecycleSettles += 1;
    },
  });

  admission.admit();
  await expect(admission.promise).resolves.toBeUndefined();
  lifecycle.reject(new Error("model failed after admission"));
  await expect(observed).resolves.toBeUndefined();

  expect(admissionFailures).toBe(0);
  expect(lifecycleSettles).toBe(1);
  expect(admission.isPending()).toBe(false);
});

test("fails closed when the submit lifecycle ends without an admission event", async () => {
  const admission = createRunAdmissionLatch();
  let admissionFailures = 0;
  let admissionError: unknown;
  const admissionFailure = expect(admission.promise).rejects.toThrow(
    "Run ended before admission was confirmed",
  );
  await monitorRunAdmissionLifecycle({
    admission,
    lifecycle: Promise.resolve(),
    onAdmissionFailure: (error) => {
      admissionFailures += 1;
      admissionError = error;
    },
    onSettled: () => undefined,
  });

  await admissionFailure;
  expect(admissionFailures).toBe(1);
  expect(isRunAdmissionNotConfirmedError(admissionError)).toBe(true);
  expect(isRunAdmissionNotConfirmedError(new Error("another failure"))).toBe(
    false,
  );
});

test("keeps stale thread attempts and callbacks isolated from the new owner", () => {
  const attemptA = createMessageSendAttempt(1, "thread-a");
  const attemptB = createMessageSendAttempt(2, "thread-b");

  expect(isCurrentMessageSendAttempt(attemptA, attemptA, "thread-a")).toBe(
    true,
  );
  attemptA.abortController.abort();
  expect(isCurrentMessageSendAttempt(attemptA, attemptA, "thread-a")).toBe(
    false,
  );
  expect(isCurrentMessageSendAttempt(attemptB, attemptA, "thread-b")).toBe(
    false,
  );
  expect(isCurrentMessageSendAttempt(attemptB, attemptB, "thread-b")).toBe(
    true,
  );
  expect(isCurrentThreadCallback("thread-a", "thread-b")).toBe(false);
  expect(isCurrentThreadCallback("thread-b", "thread-b")).toBe(true);
});

test("does not attribute a late metadata-less history error to the current view", () => {
  expect(shouldIgnoreMetadataLessStreamError(undefined, false)).toBe(true);
  expect(shouldIgnoreMetadataLessStreamError(undefined, true)).toBe(false);
  expect(shouldIgnoreMetadataLessStreamError("thread-b", false)).toBe(false);
  expect(shouldIgnoreAttributedThreadCallback(undefined, "thread-b")).toBe(
    false,
  );
  expect(shouldIgnoreAttributedThreadCallback("thread-a", "thread-b")).toBe(
    true,
  );
  expect(shouldIgnoreAttributedThreadCallback("thread-b", "thread-b")).toBe(
    false,
  );
});

test("reuses partial uploaded refs and uploads only missing attachment ids", () => {
  const cache = createUploadedAttachmentRefCache();
  const uploadedA = {
    id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
    filename: "a.png",
    size: 10,
    path: "/mnt/user-data/uploads/a.png",
    virtual_path: "/mnt/user-data/uploads/a.png",
    artifact_url: "/api/files/a",
  };
  const uploadedB = {
    ...uploadedA,
    id: "63c7cdd2-a785-41b5-9e14-b39c026f94a6",
    filename: "b.png",
    path: "/mnt/user-data/uploads/b.png",
    virtual_path: "/mnt/user-data/uploads/b.png",
    artifact_url: "/api/files/b",
  };

  rememberUploadedAttachmentRef(cache, "thread-a", "client-a", uploadedA);
  const partialRetry = planAttachmentUploadRetry(cache, "thread-a", [
    "client-a",
    "client-b",
  ]);
  expect(partialRetry.resolved).toEqual([uploadedA, undefined]);
  expect(partialRetry.missingIndexes).toEqual([1]);

  rememberUploadedAttachmentRef(cache, "thread-a", "client-b", uploadedB);
  expect(
    planAttachmentUploadRetry(cache, "thread-a", ["client-a", "client-b"]),
  ).toEqual({ resolved: [uploadedA, uploadedB], missingIndexes: [] });

  // Admission consumes only those exact attachment identities. Removing and
  // re-adding a file creates a new client id and cannot inherit an old ref.
  retainUploadedAttachmentRefs(cache, "thread-a", ["client-c"]);
  expect(planAttachmentUploadRetry(cache, "thread-a", ["client-c"])).toEqual({
    resolved: [undefined],
    missingIndexes: [0],
  });

  rememberUploadedAttachmentRef(cache, "thread-a", "client-c", uploadedA);
  forgetUploadedAttachmentRefs(cache, "thread-a", ["client-c"]);
  expect(planAttachmentUploadRetry(cache, "thread-a", ["client-c"])).toEqual({
    resolved: [undefined],
    missingIndexes: [0],
  });
});
