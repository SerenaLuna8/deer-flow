"use client";

import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  CircleIcon,
  CircleXIcon,
  Loader2Icon,
} from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type {
  ExecutionApprovalDecision,
  ExecutionApprovalProjection,
} from "@/core/execution-approvals/schemas";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

export type ExecutionApprovalCardProps = {
  approval: ExecutionApprovalProjection;
  disabled?: boolean;
  pendingDecision?: ExecutionApprovalDecision | null;
  decisionError?: string | null;
  onDecision?: (decision: ExecutionApprovalDecision) => void | Promise<void>;
};

function statusIcon(status: ExecutionApprovalProjection["status"]) {
  if (status === "approved" || status === "claimed") {
    return <Loader2Icon aria-hidden className="size-4 animate-spin" />;
  }
  if (status === "finished") {
    return <CheckCircle2Icon aria-hidden className="size-4" />;
  }
  if (
    status === "launch_failed" ||
    status === "denied" ||
    status === "expired" ||
    status === "cancelled"
  ) {
    return <CircleXIcon aria-hidden className="size-4" />;
  }
  if (status === "unknown") {
    return <AlertTriangleIcon aria-hidden className="size-4" />;
  }
  return <CircleIcon aria-hidden className="size-3 fill-current" />;
}

function statusChrome(status: ExecutionApprovalProjection["status"]): {
  border: string;
  header: string;
} {
  if (status === "pending") {
    return {
      border: "border-amber-500",
      header:
        "bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400",
    };
  }
  if (status === "approved" || status === "claimed") {
    return {
      border: "border-sky-500/70",
      header: "bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-300",
    };
  }
  if (status === "finished") {
    return {
      border: "border-emerald-500/70",
      header:
        "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300",
    };
  }
  if (status === "unknown" || status === "launch_failed") {
    return {
      border: "border-destructive/70",
      header: "bg-destructive/10 text-destructive",
    };
  }
  return {
    border: "border-border",
    header: "bg-muted/60 text-muted-foreground",
  };
}

export function ExecutionApprovalCard({
  approval,
  disabled = false,
  pendingDecision = null,
  decisionError = null,
  onDecision,
}: ExecutionApprovalCardProps) {
  const { t } = useI18n();
  const copy = t.executionApproval;
  const canDecide =
    approval.status === "pending" &&
    approval.can_decide &&
    Boolean(onDecision) &&
    !disabled;
  const decisionPending = pendingDecision !== null;
  const chrome = statusChrome(approval.status);

  const decide = (decision: ExecutionApprovalDecision) => {
    if (!canDecide || decisionPending || !onDecision) return;
    void onDecision(decision);
  };

  return (
    <section
      aria-labelledby={`execution-approval-title-${approval.approval_id}`}
      className={cn(
        "bg-card text-card-foreground overflow-hidden rounded-3xl border shadow-[0_10px_24px_-22px_rgba(15,23,42,0.45)]",
        chrome.border,
      )}
      data-execution-approval-state={approval.status}
      data-testid="execution-approval-card"
    >
      <div className={cn("flex items-center px-5 py-4 sm:px-7", chrome.header)}>
        <h2
          id={`execution-approval-title-${approval.approval_id}`}
          className="flex items-center gap-3 text-base leading-6 font-medium sm:text-lg"
        >
          <span className="flex size-4 items-center justify-center">
            {statusIcon(approval.status)}
          </span>
          {copy.statuses[approval.status]}
        </h2>
      </div>

      <div className="space-y-5 px-5 py-6 sm:px-7 sm:py-7">
        <p className="text-sm leading-6 sm:text-base sm:leading-7">
          <span className="font-medium">{copy.title}</span> {copy.riskWarning}
        </p>

        <p className="text-muted-foreground text-xs leading-5 break-words sm:text-sm">
          {approval.source_agent.label} · {approval.execution_domain.label} ·{" "}
          {copy.effectiveUser} {approval.execution_domain.effective_user_label}{" "}
          · {copy.workingDirectory}{" "}
          <span dir="ltr" style={{ unicodeBidi: "isolate" }}>
            {approval.cwd_preview}
          </span>{" "}
          · {copy.timeoutSeconds(approval.timeout_seconds)}
        </p>

        <pre
          aria-label={copy.command}
          className="text-muted-foreground max-h-80 overflow-auto font-mono text-sm leading-7 break-words whitespace-pre-wrap sm:text-base"
          dir="ltr"
          style={{ unicodeBidi: "isolate" }}
        >
          <code>{approval.command_preview}</code>
        </pre>

        {approval.status === "pending" ? (
          <p className="text-muted-foreground text-xs">
            {copy.expiresIn(approval.remaining_ttl_seconds)}
          </p>
        ) : null}
        {approval.status === "finished" ? (
          <div className="space-y-1 text-sm">
            <p>{copy.exitCode(approval.exit_code)}</p>
            <p className="text-muted-foreground text-xs">
              {copy.finishedWarning}
            </p>
          </div>
        ) : null}
        {approval.status === "launch_failed" ||
        approval.status === "expired" ||
        approval.status === "cancelled" ? (
          <p className="text-muted-foreground text-sm">
            {copy.reason}: <code>{approval.reason_code}</code>
          </p>
        ) : null}
        {approval.status === "unknown" ? (
          <Alert variant="destructive">
            <AlertTriangleIcon aria-hidden />
            <AlertTitle>{copy.unknownTitle}</AlertTitle>
            <AlertDescription>{copy.unknownWarning}</AlertDescription>
          </Alert>
        ) : null}
        {decisionError ? (
          <p className="text-destructive text-sm" role="alert">
            {decisionError}
          </p>
        ) : null}

        {approval.status === "pending" && approval.can_decide && onDecision ? (
          <div className="flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end">
            <Button
              type="button"
              variant="outline"
              className="min-h-11 rounded-full px-6 sm:min-h-9"
              disabled={!canDecide || decisionPending}
              onClick={() => decide("deny")}
            >
              {pendingDecision === "deny" ? copy.denying : copy.deny}
            </Button>
            <Button
              type="button"
              className="min-h-11 rounded-full bg-zinc-950 px-7 text-white hover:bg-zinc-800 sm:min-h-9 dark:bg-zinc-100 dark:text-zinc-950 dark:hover:bg-zinc-300"
              disabled={!canDecide || decisionPending}
              onClick={() => decide("allow_once")}
            >
              {pendingDecision === "allow_once"
                ? copy.allowing
                : copy.allowOnce}
            </Button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
