import { MEMORY_DREAM_MODEL_UNAVAILABLE_CODE } from "@/core/private-work/memory/error-presentation";
import type { MemoryDreamPreparationStatus } from "@/core/private-work/memory/types";

const MEMORY_DREAM_PREPARE_SOURCE_TOO_LARGE_CODE =
  "MEMORY_DREAM_PREPARE_SOURCE_TOO_LARGE";
const MEMORY_DREAM_PREPARE_PROMPT_BUDGET_TOO_SMALL_CODE =
  "MEMORY_DREAM_PREPARE_PROMPT_BUDGET_TOO_SMALL";

export type MemoryDreamPreparationLabelKind =
  | "queued"
  | "running"
  | "verifying"
  | "completed"
  | "cancelled"
  | "failed";

export type MemoryDreamPreparationTerminalNotice =
  | { kind: "none" }
  | { kind: "nothing_pending" }
  | { kind: "already_running" }
  | { kind: "budget_rewrite" }
  | { kind: "queued"; historyCount: number }
  | { kind: "cancelled" }
  | { kind: "model_unavailable" }
  | { kind: "source_too_large" }
  | { kind: "prompt_budget_too_small" }
  | { kind: "failed" };

export function memoryDreamPreparationLabelKind(
  preparation: MemoryDreamPreparationStatus,
): MemoryDreamPreparationLabelKind {
  if (preparation.status === "queued") return "queued";
  if (preparation.status === "running") {
    return preparation.phase === "verifying" ||
      preparation.phase === "dream_admitted"
      ? "verifying"
      : "running";
  }
  if (preparation.status === "succeeded") return "completed";
  if (preparation.status === "cancelled") return "cancelled";
  return "failed";
}

export function memoryDreamPreparationCanCancel(
  preparation: MemoryDreamPreparationStatus,
) {
  return (
    (preparation.status === "queued" || preparation.status === "running") &&
    !preparation.cancelRequested
  );
}

export function memoryDreamPreparationTerminalNotice(
  preparation: MemoryDreamPreparationStatus,
): MemoryDreamPreparationTerminalNotice {
  if (preparation.status === "queued" || preparation.status === "running") {
    return { kind: "none" };
  }
  if (preparation.status === "cancelled") return { kind: "cancelled" };
  if (
    preparation.status === "failed" &&
    preparation.publicErrorCode === MEMORY_DREAM_MODEL_UNAVAILABLE_CODE
  ) {
    return { kind: "model_unavailable" };
  }
  if (
    preparation.status === "failed" &&
    preparation.publicErrorCode === MEMORY_DREAM_PREPARE_SOURCE_TOO_LARGE_CODE
  ) {
    return { kind: "source_too_large" };
  }
  if (
    preparation.status === "failed" &&
    preparation.publicErrorCode ===
      MEMORY_DREAM_PREPARE_PROMPT_BUDGET_TOO_SMALL_CODE
  ) {
    return { kind: "prompt_budget_too_small" };
  }
  if (preparation.status === "failed") return { kind: "failed" };
  if (preparation.resultDisposition === "nothing_pending") {
    return { kind: "nothing_pending" };
  }
  if (preparation.resultDisposition === "already_running") {
    return { kind: "already_running" };
  }
  if (preparation.admissionKind === "budget_rewrite") {
    return { kind: "budget_rewrite" };
  }
  return preparation.historyCount === null
    ? { kind: "failed" }
    : { kind: "queued", historyCount: preparation.historyCount };
}
