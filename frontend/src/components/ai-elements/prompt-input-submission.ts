export type PromptInputSubmissionState = {
  inFlight: boolean;
};

export function createPromptInputSubmissionState(): PromptInputSubmissionState {
  return { inFlight: false };
}

/**
 * Runs one composer submission at a time.
 *
 * The guard is acquired synchronously, before attachment conversion or the
 * parent disabled state can re-render. A duplicate submit is ignored, while a
 * failed task releases the guard without running any success-side cleanup.
 */
export async function runExclusivePromptInputSubmission({
  state,
  disabled,
  task,
}: {
  state: PromptInputSubmissionState;
  disabled: boolean;
  task: () => Promise<void>;
}): Promise<"ignored" | "submitted"> {
  if (disabled || state.inFlight) {
    return "ignored";
  }

  state.inFlight = true;
  try {
    await task();
    return "submitted";
  } finally {
    state.inFlight = false;
  }
}
