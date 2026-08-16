import { describe, expect, test } from "@rstest/core";

import {
  createPromptInputSubmissionState,
  runExclusivePromptInputSubmission,
} from "@/components/ai-elements/prompt-input-submission";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

describe("prompt input submission transaction", () => {
  test("keeps a slow submission exclusive and runs success cleanup once", async () => {
    const state = createPromptInputSubmissionState();
    const admission = deferred<void>();
    let submitCount = 0;
    let cleanupCount = 0;
    const submit = () =>
      runExclusivePromptInputSubmission({
        state,
        disabled: false,
        task: async () => {
          submitCount += 1;
          await admission.promise;
          cleanupCount += 1;
        },
      });

    const first = submit();
    const duplicate = submit();

    expect(state.inFlight).toBe(true);
    expect(submitCount).toBe(1);
    await expect(duplicate).resolves.toBe("ignored");
    expect(cleanupCount).toBe(0);

    admission.resolve();
    await expect(first).resolves.toBe("submitted");
    expect(cleanupCount).toBe(1);
    expect(state.inFlight).toBe(false);
  });

  test("retains retry state on failure and permits one later retry", async () => {
    const state = createPromptInputSubmissionState();
    let cleanupCount = 0;

    await expect(
      runExclusivePromptInputSubmission({
        state,
        disabled: false,
        task: async () => {
          throw new Error("upload failed");
        },
      }),
    ).rejects.toThrow("upload failed");
    expect(cleanupCount).toBe(0);
    expect(state.inFlight).toBe(false);

    await expect(
      runExclusivePromptInputSubmission({
        state,
        disabled: false,
        task: async () => {
          cleanupCount += 1;
        },
      }),
    ).resolves.toBe("submitted");
    expect(cleanupCount).toBe(1);
  });

  test("does not invoke submission while the composer is disabled", async () => {
    const state = createPromptInputSubmissionState();
    let submitCount = 0;

    await expect(
      runExclusivePromptInputSubmission({
        state,
        disabled: true,
        task: async () => {
          submitCount += 1;
        },
      }),
    ).resolves.toBe("ignored");
    expect(submitCount).toBe(0);
  });
});
