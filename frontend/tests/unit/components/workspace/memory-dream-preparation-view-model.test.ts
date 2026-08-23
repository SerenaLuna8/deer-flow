import { describe, expect, test } from "@rstest/core";

import {
  memoryDreamPreparationCanCancel,
  memoryDreamPreparationLabelKind,
  memoryDreamPreparationTerminalNotice,
} from "@/components/workspace/memory-dream-preparation-view-model";
import type { MemoryDreamPreparationStatus } from "@/core/private-work/memory/types";

const BASE_STATUS: MemoryDreamPreparationStatus = {
  jobId: "33333333-3333-4333-8333-333333333333",
  status: "running",
  phase: "draining",
  compactedPasses: 2,
  dreamJobId: null,
  historyCount: null,
  admissionKind: null,
  resultDisposition: "queued",
  cancelRequested: false,
  publicErrorCode: null,
  updatedAt: "2026-08-13T00:00:00Z",
};

function status(
  overrides: Partial<MemoryDreamPreparationStatus>,
): MemoryDreamPreparationStatus {
  return { ...BASE_STATUS, ...overrides };
}

describe("Memory Dream preparation view model", () => {
  test("maps durable phases to discrete progress labels", () => {
    expect(
      memoryDreamPreparationLabelKind(
        status({ status: "queued", phase: "queued" }),
      ),
    ).toBe("queued");
    expect(memoryDreamPreparationLabelKind(BASE_STATUS)).toBe("running");
    expect(
      memoryDreamPreparationLabelKind(status({ phase: "verifying" })),
    ).toBe("verifying");
    expect(
      memoryDreamPreparationLabelKind(status({ phase: "dream_admitted" })),
    ).toBe("verifying");
    expect(
      memoryDreamPreparationLabelKind(
        status({ status: "succeeded", phase: "succeeded" }),
      ),
    ).toBe("completed");
    expect(
      memoryDreamPreparationLabelKind(
        status({ status: "cancelled", phase: "cancelled" }),
      ),
    ).toBe("cancelled");
    expect(
      memoryDreamPreparationLabelKind(
        status({ status: "failed", phase: "failed" }),
      ),
    ).toBe("failed");
  });

  test("disables cooperative cancel once requested or terminal", () => {
    expect(memoryDreamPreparationCanCancel(BASE_STATUS)).toBe(true);
    expect(
      memoryDreamPreparationCanCancel(status({ cancelRequested: true })),
    ).toBe(false);
    expect(
      memoryDreamPreparationCanCancel(
        status({ status: "succeeded", phase: "succeeded" }),
      ),
    ).toBe(false);
    expect(
      memoryDreamPreparationCanCancel(
        status({ status: "cancelled", phase: "cancelled" }),
      ),
    ).toBe(false);
  });

  test("keeps active work quiet and distinguishes every terminal outcome", () => {
    expect(memoryDreamPreparationTerminalNotice(BASE_STATUS)).toEqual({
      kind: "none",
    });
    expect(
      memoryDreamPreparationTerminalNotice(
        status({
          status: "succeeded",
          phase: "succeeded",
          resultDisposition: "nothing_pending",
          historyCount: 0,
        }),
      ),
    ).toEqual({ kind: "nothing_pending" });
    expect(
      memoryDreamPreparationTerminalNotice(
        status({
          status: "succeeded",
          phase: "succeeded",
          resultDisposition: "already_running",
          historyCount: 4,
        }),
      ),
    ).toEqual({ kind: "already_running" });
    expect(
      memoryDreamPreparationTerminalNotice(
        status({
          status: "succeeded",
          phase: "succeeded",
          admissionKind: "budget_rewrite",
          historyCount: 0,
        }),
      ),
    ).toEqual({ kind: "budget_rewrite" });
    expect(
      memoryDreamPreparationTerminalNotice(
        status({
          status: "succeeded",
          phase: "succeeded",
          admissionKind: "history",
          historyCount: 7,
        }),
      ),
    ).toEqual({ kind: "queued", historyCount: 7 });
    expect(
      memoryDreamPreparationTerminalNotice(
        status({ status: "cancelled", phase: "cancelled" }),
      ),
    ).toEqual({ kind: "cancelled" });
    expect(
      memoryDreamPreparationTerminalNotice(
        status({ status: "failed", phase: "failed" }),
      ),
    ).toEqual({ kind: "failed" });
  });

  test("fails closed instead of rendering an invented zero-count success", () => {
    expect(
      memoryDreamPreparationTerminalNotice(
        status({
          status: "succeeded",
          phase: "succeeded",
          historyCount: null,
        }),
      ),
    ).toEqual({ kind: "failed" });
  });

  test("distinguishes an unavailable Dream model from a generic failure", () => {
    expect(
      memoryDreamPreparationTerminalNotice(
        status({
          status: "failed",
          phase: "failed",
          publicErrorCode: "MEMORY_DREAM_MODEL_UNAVAILABLE",
        }),
      ),
    ).toEqual({ kind: "model_unavailable" });
  });

  test("distinguishes permanent compaction planning failures", () => {
    expect(
      memoryDreamPreparationTerminalNotice(
        status({
          status: "failed",
          phase: "failed",
          publicErrorCode: "MEMORY_DREAM_PREPARE_SOURCE_TOO_LARGE",
        }),
      ),
    ).toEqual({ kind: "source_too_large" });
    expect(
      memoryDreamPreparationTerminalNotice(
        status({
          status: "failed",
          phase: "failed",
          publicErrorCode: "MEMORY_DREAM_PREPARE_PROMPT_BUDGET_TOO_SMALL",
        }),
      ),
    ).toEqual({ kind: "prompt_budget_too_small" });
  });
});
