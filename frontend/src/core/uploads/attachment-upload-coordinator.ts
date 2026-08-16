import type { UploadedFileInfo } from "./api";

export type AttachmentUploadCandidate = {
  clientId: string;
  file: File;
};

export type AttachmentUploadStatus = "uploading" | "ready" | "error";

export type AttachmentUploadBatch = (
  files: File[],
  onFileUploaded: (uploaded: UploadedFileInfo, index: number) => void,
) => Promise<void>;

export type DiscardedAttachmentCleanup = (uploaded: UploadedFileInfo) => void;

type EnsureAttachmentUploadsOptions = {
  /** Exact account/project/thread identity for this upload. */
  scopeKey: string;
  candidates: AttachmentUploadCandidate[];
  retryPendingFailure: boolean;
  signal?: AbortSignal;
  upload: AttachmentUploadBatch;
  onStatusChange?: (clientId: string, status: AttachmentUploadStatus) => void;
};

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
  reject: (reason?: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: Deferred<T>["resolve"];
  let reject!: Deferred<T>["reject"];
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

function uniquePendingTasks(
  pending: Map<string, Promise<void>> | undefined,
  clientIds: readonly string[],
): Promise<void>[] {
  if (!pending) return [];
  return [
    ...new Set(
      clientIds.flatMap((clientId) => {
        const task = pending.get(clientId);
        return task ? [task] : [];
      }),
    ),
  ];
}

/**
 * Coordinates eager composer uploads with the send path.
 *
 * A ready file is scoped to its exact account, Project, Thread, and browser
 * attachment identity. Concurrent callers wait for the same pending batch, so
 * pressing Send while an eager upload is in flight never starts a duplicate
 * POST. A send claims its snapshot until Run admission, preventing a late
 * remove click from deleting a file that the submitted message references.
 */
export class AttachmentUploadCoordinator {
  private readonly ready = new Map<string, Map<string, UploadedFileInfo>>();

  private readonly pending = new Map<string, Map<string, Promise<void>>>();

  private readonly generations = new Map<string, number>();

  private readonly discarded = new Map<string, Set<string>>();

  private readonly discardedCleanups = new Map<
    string,
    Map<string, DiscardedAttachmentCleanup>
  >();

  private readonly claimed = new Map<string, Set<string>>();

  private readonly cleanupOnRelease = new Map<
    string,
    Map<string, DiscardedAttachmentCleanup>
  >();

  private readonly abandonedCleanups = new Map<
    string,
    Map<string, Map<Promise<void>, DiscardedAttachmentCleanup>>
  >();

  private generation(scopeKey: string): number {
    return this.generations.get(scopeKey) ?? 0;
  }

  private readReady(
    scopeKey: string,
    clientId: string,
  ): UploadedFileInfo | undefined {
    return this.ready.get(scopeKey)?.get(clientId);
  }

  private rememberReady(
    scopeKey: string,
    clientId: string,
    uploaded: UploadedFileInfo,
  ): void {
    let scopeReady = this.ready.get(scopeKey);
    if (!scopeReady) {
      scopeReady = new Map();
      this.ready.set(scopeKey, scopeReady);
    }
    scopeReady.set(clientId, uploaded);
  }

  private isDiscarded(scopeKey: string, clientId: string): boolean {
    return this.discarded.get(scopeKey)?.has(clientId) === true;
  }

  private isClaimed(scopeKey: string, clientId: string): boolean {
    return this.claimed.get(scopeKey)?.has(clientId) === true;
  }

  private cleanupDiscardedUpload(
    scopeKey: string,
    clientId: string,
    uploaded: UploadedFileInfo,
  ): void {
    const scopeCleanups = this.discardedCleanups.get(scopeKey);
    const cleanup = scopeCleanups?.get(clientId);
    scopeCleanups?.delete(clientId);
    if (scopeCleanups?.size === 0) {
      this.discardedCleanups.delete(scopeKey);
    }
    cleanup?.(uploaded);
  }

  private rememberAbandonedCleanup(
    scopeKey: string,
    clientId: string,
    task: Promise<void>,
    cleanup: DiscardedAttachmentCleanup,
  ): void {
    let scopeCleanups = this.abandonedCleanups.get(scopeKey);
    if (!scopeCleanups) {
      scopeCleanups = new Map();
      this.abandonedCleanups.set(scopeKey, scopeCleanups);
    }
    let attachmentCleanups = scopeCleanups.get(clientId);
    if (!attachmentCleanups) {
      attachmentCleanups = new Map();
      scopeCleanups.set(clientId, attachmentCleanups);
    }
    attachmentCleanups.set(task, cleanup);
  }

  private takeAbandonedCleanup(
    scopeKey: string,
    clientId: string,
    task: Promise<void>,
  ): DiscardedAttachmentCleanup | undefined {
    const scopeCleanups = this.abandonedCleanups.get(scopeKey);
    const attachmentCleanups = scopeCleanups?.get(clientId);
    const cleanup = attachmentCleanups?.get(task);
    attachmentCleanups?.delete(task);
    if (attachmentCleanups?.size === 0) scopeCleanups?.delete(clientId);
    if (scopeCleanups?.size === 0) this.abandonedCleanups.delete(scopeKey);
    return cleanup;
  }

  private forgetAbandonedTask(
    scopeKey: string,
    clientIds: readonly string[],
    task: Promise<void>,
  ): void {
    for (const clientId of clientIds) {
      this.takeAbandonedCleanup(scopeKey, clientId, task);
    }
  }

  private forgetPendingTask(
    scopeKey: string,
    clientIds: readonly string[],
    task: Promise<void>,
  ): void {
    const scopePending = this.pending.get(scopeKey);
    if (!scopePending) return;
    for (const clientId of clientIds) {
      if (scopePending.get(clientId) === task) {
        scopePending.delete(clientId);
      }
    }
    if (scopePending.size === 0) {
      this.pending.delete(scopeKey);
    }
  }

  private startBatch({
    scopeKey,
    candidates,
    upload,
    onStatusChange,
  }: Pick<
    EnsureAttachmentUploadsOptions,
    "scopeKey" | "candidates" | "upload" | "onStatusChange"
  >): Promise<void> {
    const slot = deferred<void>();
    const task = slot.promise;
    const clientIds = candidates.map((candidate) => candidate.clientId);
    const generation = this.generation(scopeKey);
    let scopePending = this.pending.get(scopeKey);
    if (!scopePending) {
      scopePending = new Map();
      this.pending.set(scopeKey, scopePending);
    }
    for (const clientId of clientIds) {
      scopePending.set(clientId, task);
      onStatusChange?.(clientId, "uploading");
    }

    let uploadResult: Promise<void>;
    try {
      uploadResult = upload(
        candidates.map((candidate) => candidate.file),
        (uploaded, index) => {
          const candidate = candidates[index];
          if (!candidate) return;

          const abandonedCleanup = this.takeAbandonedCleanup(
            scopeKey,
            candidate.clientId,
            task,
          );
          if (abandonedCleanup) {
            abandonedCleanup(uploaded);
            return;
          }
          if (this.pending.get(scopeKey)?.get(candidate.clientId) !== task) {
            return;
          }
          if (
            this.generation(scopeKey) !== generation &&
            !this.isClaimed(scopeKey, candidate.clientId)
          ) {
            return;
          }
          if (this.isDiscarded(scopeKey, candidate.clientId)) {
            this.cleanupDiscardedUpload(scopeKey, candidate.clientId, uploaded);
            return;
          }
          this.rememberReady(scopeKey, candidate.clientId, uploaded);
          onStatusChange?.(candidate.clientId, "ready");
        },
      );
    } catch (error) {
      uploadResult = Promise.reject(
        error instanceof Error ? error : new Error("Attachment upload failed."),
      );
    }

    void uploadResult.then(
      () => {
        this.forgetPendingTask(scopeKey, clientIds, task);
        this.forgetAbandonedTask(scopeKey, clientIds, task);
        slot.resolve();
      },
      (error: unknown) => {
        this.forgetPendingTask(scopeKey, clientIds, task);
        this.forgetAbandonedTask(scopeKey, clientIds, task);
        if (this.generation(scopeKey) === generation) {
          for (const clientId of clientIds) {
            if (
              !this.readReady(scopeKey, clientId) &&
              !this.isDiscarded(scopeKey, clientId)
            ) {
              onStatusChange?.(clientId, "error");
            }
          }
        }
        slot.reject(error);
      },
    );

    return task;
  }

  async ensure({
    scopeKey,
    candidates,
    retryPendingFailure,
    signal,
    upload,
    onStatusChange,
  }: EnsureAttachmentUploadsOptions): Promise<UploadedFileInfo[]> {
    signal?.throwIfAborted();
    if (candidates.length === 0) return [];

    const clientIds = candidates.map((candidate) => candidate.clientId);
    if (new Set(clientIds).size !== clientIds.length) {
      throw new Error("Attachment client ids must be unique within a message.");
    }

    let mayStartBatch = true;
    for (;;) {
      signal?.throwIfAborted();
      if (clientIds.some((clientId) => this.isDiscarded(scopeKey, clientId))) {
        const error = new Error("The attachment was removed before upload.");
        error.name = "AbortError";
        throw error;
      }
      const resolved = clientIds.map((clientId) =>
        this.readReady(scopeKey, clientId),
      );
      const missingIndexes = resolved.flatMap((uploaded, index) =>
        uploaded ? [] : [index],
      );
      if (missingIndexes.length === 0) {
        for (const clientId of clientIds) {
          onStatusChange?.(clientId, "ready");
        }
        return resolved as UploadedFileInfo[];
      }

      const missingClientIds = missingIndexes.map(
        (index) => candidates[index]!.clientId,
      );
      let tasks = uniquePendingTasks(
        this.pending.get(scopeKey),
        missingClientIds,
      );

      if (tasks.length === 0) {
        if (!mayStartBatch) {
          for (const clientId of missingClientIds) {
            onStatusChange?.(clientId, "error");
          }
          throw new Error("An uploaded attachment reference is missing.");
        }
        mayStartBatch = false;
        tasks = [
          this.startBatch({
            scopeKey,
            candidates: missingIndexes.map((index) => candidates[index]!),
            upload,
            onStatusChange,
          }),
        ];
      } else {
        for (const clientId of missingClientIds) {
          onStatusChange?.(clientId, "uploading");
        }
      }

      const settled = await Promise.allSettled(tasks);
      signal?.throwIfAborted();
      const failure = settled.find(
        (result): result is PromiseRejectedResult =>
          result.status === "rejected",
      );
      if (failure) {
        const unresolvedClientIds = missingClientIds.filter(
          (clientId) => !this.readReady(scopeKey, clientId),
        );
        for (const clientId of unresolvedClientIds) {
          onStatusChange?.(clientId, "error");
        }
        if (!retryPendingFailure || !mayStartBatch) {
          throw failure.reason;
        }
      }
    }
  }

  claim(scopeKey: string, clientIds: readonly string[]): boolean {
    if (
      clientIds.some(
        (clientId) =>
          this.isDiscarded(scopeKey, clientId) ||
          this.isClaimed(scopeKey, clientId),
      )
    ) {
      return false;
    }
    let scopeClaims = this.claimed.get(scopeKey);
    if (!scopeClaims) {
      scopeClaims = new Set();
      this.claimed.set(scopeKey, scopeClaims);
    }
    for (const clientId of clientIds) scopeClaims.add(clientId);
    return true;
  }

  release(scopeKey: string, clientIds: readonly string[]): void {
    const scopeClaims = this.claimed.get(scopeKey);
    const releaseCleanups = this.cleanupOnRelease.get(scopeKey);
    for (const clientId of clientIds) {
      if (!scopeClaims?.delete(clientId)) continue;
      const cleanup = releaseCleanups?.get(clientId);
      releaseCleanups?.delete(clientId);
      if (!cleanup) continue;

      const uploaded = this.readReady(scopeKey, clientId);
      if (uploaded) cleanup(uploaded);
      this.ready.get(scopeKey)?.delete(clientId);

      const task = this.pending.get(scopeKey)?.get(clientId);
      if (task) {
        this.rememberAbandonedCleanup(scopeKey, clientId, task, cleanup);
        this.pending.get(scopeKey)?.delete(clientId);
      }
    }
    if (scopeClaims?.size === 0) this.claimed.delete(scopeKey);
    if (releaseCleanups?.size === 0) this.cleanupOnRelease.delete(scopeKey);
    if (this.ready.get(scopeKey)?.size === 0) this.ready.delete(scopeKey);
    if (this.pending.get(scopeKey)?.size === 0) this.pending.delete(scopeKey);
  }

  consume(scopeKey: string, clientIds: readonly string[]): void {
    const scopeReady = this.ready.get(scopeKey);
    const scopePending = this.pending.get(scopeKey);
    const scopeDiscarded = this.discarded.get(scopeKey);
    const scopeCleanups = this.discardedCleanups.get(scopeKey);
    const scopeClaims = this.claimed.get(scopeKey);
    const releaseCleanups = this.cleanupOnRelease.get(scopeKey);
    for (const clientId of clientIds) {
      scopeReady?.delete(clientId);
      scopePending?.delete(clientId);
      scopeDiscarded?.delete(clientId);
      scopeCleanups?.delete(clientId);
      scopeClaims?.delete(clientId);
      releaseCleanups?.delete(clientId);
    }
    if (scopeReady?.size === 0) this.ready.delete(scopeKey);
    if (scopePending?.size === 0) this.pending.delete(scopeKey);
    if (scopeDiscarded?.size === 0) this.discarded.delete(scopeKey);
    if (scopeCleanups?.size === 0) this.discardedCleanups.delete(scopeKey);
    if (scopeClaims?.size === 0) this.claimed.delete(scopeKey);
    if (releaseCleanups?.size === 0) this.cleanupOnRelease.delete(scopeKey);
  }

  discard(
    scopeKey: string,
    clientId: string,
    cleanup: DiscardedAttachmentCleanup,
  ): boolean {
    if (this.isClaimed(scopeKey, clientId)) return false;

    let scopeDiscarded = this.discarded.get(scopeKey);
    if (!scopeDiscarded) {
      scopeDiscarded = new Set();
      this.discarded.set(scopeKey, scopeDiscarded);
    }
    scopeDiscarded.add(clientId);

    let scopeCleanups = this.discardedCleanups.get(scopeKey);
    if (!scopeCleanups) {
      scopeCleanups = new Map();
      this.discardedCleanups.set(scopeKey, scopeCleanups);
    }
    scopeCleanups.set(clientId, cleanup);

    const uploaded = this.readReady(scopeKey, clientId);
    if (uploaded) {
      this.ready.get(scopeKey)?.delete(clientId);
      if (this.ready.get(scopeKey)?.size === 0) {
        this.ready.delete(scopeKey);
      }
      this.cleanupDiscardedUpload(scopeKey, clientId, uploaded);
    }
    return true;
  }

  resetScope(scopeKey: string, cleanup: DiscardedAttachmentCleanup): void {
    const scopeClaims = this.claimed.get(scopeKey);
    const scopeReady = this.ready.get(scopeKey);
    const scopePending = this.pending.get(scopeKey);
    let releaseCleanups = this.cleanupOnRelease.get(scopeKey);
    for (const [clientId, uploaded] of scopeReady ?? []) {
      if (scopeClaims?.has(clientId)) {
        releaseCleanups ??= new Map();
        releaseCleanups.set(clientId, cleanup);
      } else {
        cleanup(uploaded);
        scopeReady?.delete(clientId);
      }
    }
    for (const [clientId, task] of scopePending ?? []) {
      if (scopeClaims?.has(clientId)) {
        releaseCleanups ??= new Map();
        releaseCleanups.set(clientId, cleanup);
      } else {
        this.rememberAbandonedCleanup(scopeKey, clientId, task, cleanup);
        scopePending?.delete(clientId);
      }
    }
    for (const clientId of scopeClaims ?? []) {
      releaseCleanups ??= new Map();
      releaseCleanups.set(clientId, cleanup);
    }
    if (releaseCleanups) {
      this.cleanupOnRelease.set(scopeKey, releaseCleanups);
    }

    this.generations.set(scopeKey, this.generation(scopeKey) + 1);
    if (scopeReady?.size === 0) this.ready.delete(scopeKey);
    if (scopePending?.size === 0) this.pending.delete(scopeKey);
    this.discarded.delete(scopeKey);
    this.discardedCleanups.delete(scopeKey);
  }
}
