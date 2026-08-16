import { expect, test } from "@rstest/core";

import {
  awaitAbortableSidecarPreparation,
  canClaimSidecarQueue,
  consumeSidecarQueue,
  createSidecarIdentity,
  createSidecarQueueSettlement,
  settleSidecarQueueSubmission,
} from "@/core/sidecar";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

test("keeps a queued first sidecar submit pending until real admission", async () => {
  const settlement = createSidecarQueueSettlement();
  const admission = deferred<void>();
  const forwarding = settleSidecarQueueSubmission(
    settlement,
    () => admission.promise,
  );
  let settled = false;
  void settlement.promise.then(() => {
    settled = true;
  });

  await Promise.resolve();
  expect(settled).toBe(false);
  admission.resolve();
  await forwarding;
  await expect(settlement.promise).resolves.toBeUndefined();
  expect(settled).toBe(true);
});

test("propagates queued sidecar admission failure to the composer", async () => {
  const settlement = createSidecarQueueSettlement();
  const admissionFailure = expect(settlement.promise).rejects.toThrow(
    "admission failed",
  );
  await settleSidecarQueueSubmission(settlement, async () => {
    throw new Error("admission failed");
  });
  await admissionFailure;
  expect(settlement.isPending()).toBe(false);
});

test("returns a stale queued settlement so drop and unmount can reject it", async () => {
  const identityA = createSidecarIdentity("parent-a");
  const identityB = createSidecarIdentity("parent-b");
  const settlement = createSidecarQueueSettlement();
  const queued = {
    identity: identityA,
    threadId: "sidecar-a",
    value: { settlement },
  };
  const decision = consumeSidecarQueue({
    currentIdentity: identityB,
    queued,
    sidecarThreadId: null,
    boundThreadId: null,
  });

  expect(decision.action).toBe("drop");
  if (decision.action !== "drop") throw new Error("expected drop");
  const dropped = expect(settlement.promise).rejects.toThrow("changed");
  decision.queued.value.settlement.reject(
    new Error("side conversation changed"),
  );
  await dropped;
});

test("claims one queued effect closure at most once", () => {
  const identity = createSidecarIdentity("parent-a");
  const queued = {
    identity,
    threadId: "sidecar-a",
    value: { settlement: createSidecarQueueSettlement() },
  };

  expect(canClaimSidecarQueue(queued, queued)).toBe(true);
  expect(canClaimSidecarQueue(null, queued)).toBe(false);
});

test("unmount aborts a sidecar submit while thread preparation is pending", async () => {
  const preparation = deferred<string>();
  const controller = new AbortController();
  const pending = awaitAbortableSidecarPreparation(
    controller.signal,
    () => preparation.promise,
  );
  const error = new Error("side conversation closed before admission");
  error.name = "AbortError";

  controller.abort(error);

  await expect(pending).rejects.toMatchObject({
    name: "AbortError",
    message: "side conversation closed before admission",
  });

  // The non-abortable network request may still settle later, but the caller
  // has already been released and that late value cannot revive the attempt.
  preparation.resolve("late-sidecar-thread");
  await Promise.resolve();
});
