import type { Message } from "@langchain/langgraph-sdk";
import { z } from "zod";

import type { FileInMessage } from "../messages/utils";
import type { UploadedFileInfo } from "../uploads";

export type MessageSendAttempt = {
  readonly generation: number;
  readonly threadId: string;
  readonly abortController: AbortController;
};

export function createMessageSendAttempt(
  generation: number,
  threadId: string,
): MessageSendAttempt {
  return {
    generation,
    threadId,
    abortController: new AbortController(),
  };
}

export function isCurrentMessageSendAttempt(
  current: MessageSendAttempt | null,
  candidate: MessageSendAttempt,
  currentViewThreadId: string | null,
): boolean {
  return (
    current === candidate &&
    !candidate.abortController.signal.aborted &&
    candidate.threadId === currentViewThreadId
  );
}

export function staleMessageSendError(): Error {
  const error = new Error(
    "The active thread changed before the message was admitted.",
  );
  error.name = "AbortError";
  return error;
}

export type UploadedAttachmentRefCache = Map<
  string,
  Map<string, UploadedFileInfo>
>;

export function createUploadedAttachmentRefCache(): UploadedAttachmentRefCache {
  return new Map();
}

export function readUploadedAttachmentRef(
  cache: UploadedAttachmentRefCache,
  threadId: string,
  clientId: string | undefined,
): UploadedFileInfo | undefined {
  return clientId ? cache.get(threadId)?.get(clientId) : undefined;
}

export function rememberUploadedAttachmentRef(
  cache: UploadedAttachmentRefCache,
  threadId: string,
  clientId: string | undefined,
  uploaded: UploadedFileInfo,
): void {
  if (!clientId) return;
  let threadCache = cache.get(threadId);
  if (!threadCache) {
    threadCache = new Map();
    cache.set(threadId, threadCache);
  }
  threadCache.set(clientId, uploaded);
}

export function forgetUploadedAttachmentRefs(
  cache: UploadedAttachmentRefCache,
  threadId: string,
  clientIds: readonly (string | undefined)[],
): void {
  const threadCache = cache.get(threadId);
  if (!threadCache) return;
  for (const clientId of clientIds) {
    if (clientId) threadCache.delete(clientId);
  }
  if (threadCache.size === 0) cache.delete(threadId);
}

export function retainUploadedAttachmentRefs(
  cache: UploadedAttachmentRefCache,
  threadId: string,
  clientIds: readonly (string | undefined)[],
): void {
  const threadCache = cache.get(threadId);
  if (!threadCache) return;
  const retained = new Set(
    clientIds.filter((clientId): clientId is string => Boolean(clientId)),
  );
  for (const clientId of threadCache.keys()) {
    if (!retained.has(clientId)) threadCache.delete(clientId);
  }
  if (threadCache.size === 0) cache.delete(threadId);
}

export function planAttachmentUploadRetry(
  cache: UploadedAttachmentRefCache,
  threadId: string,
  clientIds: readonly (string | undefined)[],
): {
  resolved: Array<UploadedFileInfo | undefined>;
  missingIndexes: number[];
} {
  const resolved = clientIds.map((clientId) =>
    readUploadedAttachmentRef(cache, threadId, clientId),
  );
  return {
    resolved,
    missingIndexes: resolved.flatMap((uploaded, index) =>
      uploaded ? [] : [index],
    ),
  };
}

export function isCurrentThreadCallback(
  callbackThreadId: string,
  currentViewThreadId: string | null,
): boolean {
  return callbackThreadId === currentViewThreadId;
}

export function shouldIgnoreAttributedThreadCallback(
  callbackThreadId: string | undefined,
  currentViewThreadId: string | null,
): boolean {
  return (
    callbackThreadId !== undefined &&
    !isCurrentThreadCallback(callbackThreadId, currentViewThreadId)
  );
}

export function shouldIgnoreMetadataLessStreamError(
  callbackThreadId: string | undefined,
  hasPreparedReplayAttribution: boolean,
): boolean {
  // The SDK emits history fetch/mutate failures without callback metadata,
  // including late failures from a previously viewed thread. They cannot be
  // attributed safely and must never clear/toast the current projection.
  // Prepared replay is the one exception: it opens an explicit synchronous
  // attribution window and has dedicated classification/rollback handling.
  return callbackThreadId === undefined && !hasPreparedReplayAttribution;
}

export type SendMessageOptions = {
  additionalKwargs?: Record<string, unknown>;
  additionalInputMessages?: Message[];
  continueFromLatestCheckpoint?: boolean;
  /**
   * Invoked exactly once after uploads succeed and the server admits the Run.
   * It never fires for a dropped concurrent send or pre-admission failure, so
   * callers can safely clear recoverable one-time composer state. A later Run
   * terminal failure does not undo this callback.
   */
  onSent?: () => void;
};

export type RunAdmissionLatch = {
  readonly promise: Promise<void>;
  isPending: () => boolean;
  admit: () => boolean;
  reject: (error: unknown) => boolean;
};

/**
 * Separates server Run admission from the SDK submit Promise, which settles at
 * the Run terminal. Composer state must clear on `onCreated` (admission), not
 * after the Agent finishes.
 */
export function createRunAdmissionLatch(): RunAdmissionLatch {
  let pending = true;
  let resolvePromise!: () => void;
  let rejectPromise!: (error: unknown) => void;
  const promise = new Promise<void>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });

  return {
    promise,
    isPending: () => pending,
    admit: () => {
      if (!pending) return false;
      pending = false;
      resolvePromise();
      return true;
    },
    reject: (error) => {
      if (!pending) return false;
      pending = false;
      rejectPromise(error);
      return true;
    },
  };
}

/**
 * Observes the SDK's terminal-scoped submit Promise without leaking a rejected
 * background Promise. A failure before `admit()` rejects the recoverable
 * composer submission; a terminal failure after admission is consumed here and
 * must not resurrect the already-cleared draft.
 */
export async function monitorRunAdmissionLifecycle({
  admission,
  lifecycle,
  onAdmissionFailure,
  onSettled,
}: {
  admission: RunAdmissionLatch;
  lifecycle: Promise<void>;
  onAdmissionFailure: (error: unknown) => void;
  onSettled: () => void;
}): Promise<void> {
  try {
    await lifecycle;
    if (admission.isPending()) {
      const error = new Error("The Run ended before admission was confirmed.");
      admission.reject(error);
      onAdmissionFailure(error);
    }
  } catch (error) {
    if (admission.isPending()) {
      admission.reject(error);
      onAdmissionFailure(error);
    }
  } finally {
    onSettled();
  }
}

/** Admission is server-owned; best-effort UI observers cannot veto it. */
export function admitRunAndNotify(
  admission: RunAdmissionLatch,
  ...observers: Array<(() => void) | undefined>
): boolean {
  const admitted = admission.admit();
  for (const observer of observers) {
    try {
      observer?.();
    } catch {
      // A local observer must never turn an accepted Run into a failed submit.
    }
  }
  return admitted;
}

export function buildThreadSubmitCheckpointOptions(
  continueFromLatestCheckpoint: boolean | undefined,
): { checkpoint: null } | Record<string, never> {
  return continueFromLatestCheckpoint ? { checkpoint: null } : {};
}

export function buildThreadSubmitMessages({
  text,
  messageId,
  additionalKwargs,
  additionalInputMessages = [],
  filesForSubmit = [],
}: {
  text: string;
  messageId: string;
  additionalKwargs?: Record<string, unknown>;
  additionalInputMessages?: Message[];
  filesForSubmit?: FileInMessage[];
}): Message[] {
  return [
    ...additionalInputMessages,
    {
      type: "human",
      id: messageId,
      content: [
        {
          type: "text",
          text,
        },
      ],
      additional_kwargs: {
        ...additionalKwargs,
        ...(filesForSubmit.length > 0 ? { files: filesForSubmit } : {}),
      },
    } as Message,
  ];
}

export function uploadedFileInfoToMessage(
  info: UploadedFileInfo,
): FileInMessage {
  if (!info.id) {
    throw new TypeError("Uploaded file response is missing its opaque id");
  }
  if (!z.string().uuid().safeParse(info.id).success) {
    throw new TypeError("Uploaded file response has an invalid opaque id");
  }
  return {
    file_id: info.id,
    filename: info.filename,
    size: info.size,
    path: info.virtual_path,
    status: "uploaded",
  };
}
