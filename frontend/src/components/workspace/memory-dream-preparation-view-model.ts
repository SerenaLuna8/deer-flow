import type { MemoryDreamPreparationStatus } from "@/core/private-work/memory/types";

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
