import {
  AlertCircleIcon,
  CheckIcon,
  CopyIcon,
  InboxIcon,
  RefreshCwIcon,
} from "lucide-react";
import { useState, type ComponentProps, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export type StatusTone = "danger" | "healthy" | "neutral" | "warning";

const HEALTHY_STATUSES = new Set([
  "active",
  "owned",
  "ready",
  "safe",
  "success",
  "succeeded",
]);
const WARNING_STATUSES = new Set([
  "degraded",
  "leased",
  "pending_deletion",
  "polling",
  "queued",
  "retry_wait",
  "running",
  "suspended",
  "unknown",
  "unowned",
]);
const DANGER_STATUSES = new Set([
  "closed",
  "dead",
  "failed",
  "ownership_lost",
  "rejected",
  "unavailable",
  "unsafe",
]);

export function adminStatusTone(status: string): StatusTone {
  if (HEALTHY_STATUSES.has(status)) return "healthy";
  if (WARNING_STATUSES.has(status)) return "warning";
  if (DANGER_STATUSES.has(status)) return "danger";
  return "neutral";
}

export function AdminStatus({
  children,
  className,
  status,
  tone,
}: {
  children: ReactNode;
  className?: string;
  status: string;
  tone?: StatusTone;
}) {
  const resolvedTone = tone ?? adminStatusTone(status);
  return (
    <span
      data-slot="admin-status"
      data-status={status}
      data-tone={resolvedTone}
      className={cn(
        "inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
        resolvedTone === "healthy" &&
          "border-success/30 bg-success/10 text-foreground",
        resolvedTone === "warning" &&
          "border-chart-4/30 bg-chart-4/10 text-foreground",
        resolvedTone === "danger" &&
          "border-destructive/25 bg-destructive/10 text-destructive",
        resolvedTone === "neutral" &&
          "border-border bg-muted text-muted-foreground",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-1.5 rounded-full",
          resolvedTone === "healthy" && "bg-success",
          resolvedTone === "warning" && "bg-chart-4",
          resolvedTone === "danger" && "bg-destructive",
          resolvedTone === "neutral" && "bg-muted-foreground/60",
        )}
      />
      {children}
    </span>
  );
}

export function AdminLoadingState({
  className,
  label,
}: {
  className?: string;
  label: string;
}) {
  return (
    <section
      data-slot="admin-loading-state"
      aria-busy="true"
      aria-label={label}
      className={cn(
        "border-border/80 bg-card overflow-hidden rounded-lg border",
        className,
      )}
    >
      <div className="border-border/70 flex items-center gap-2 border-b px-5 py-4">
        <span className="bg-muted-foreground/40 size-1.5 animate-pulse rounded-full" />
        <p className="text-muted-foreground text-sm font-medium">{label}</p>
      </div>
      <div className="space-y-2.5 p-4">
        <Skeleton className="h-9 w-2/5 rounded-md" />
        <Skeleton className="h-11 w-full rounded-md" />
        <Skeleton className="h-11 w-full rounded-md" />
      </div>
    </section>
  );
}

export function AdminErrorState({
  description,
  onRetry,
  retryLabel,
  title,
}: {
  description: string;
  onRetry?: () => void;
  retryLabel: string;
  title: string;
}) {
  return (
    <section
      data-slot="admin-error-state"
      role="alert"
      className="border-destructive/20 bg-destructive/5 rounded-lg border px-5 py-6 text-center"
    >
      <span className="border-destructive/20 bg-background text-destructive mx-auto flex size-10 items-center justify-center rounded-full border">
        <AlertCircleIcon aria-hidden className="size-5" />
      </span>
      <h2 className="mt-4 text-sm font-semibold">{title}</h2>
      <p className="text-muted-foreground mx-auto mt-1 max-w-lg text-sm leading-6">
        {description}
      </p>
      {onRetry ? (
        <Button
          className="mt-4"
          type="button"
          variant="outline"
          size="sm"
          onClick={onRetry}
        >
          <RefreshCwIcon aria-hidden className="size-3.5" />
          {retryLabel}
        </Button>
      ) : null}
    </section>
  );
}

export function AdminEmptyState({
  action,
  description,
  variant = "panel",
  title,
}: {
  action?: ReactNode;
  description: string;
  title: string;
  variant?: "inline" | "panel";
}) {
  return (
    <section
      data-slot="admin-empty-state"
      className={cn(
        "px-5 py-7 text-center",
        variant === "panel" && "border-border/80 bg-card rounded-lg border",
      )}
    >
      <span className="border-border bg-muted text-muted-foreground mx-auto flex size-9 items-center justify-center rounded-full border">
        <InboxIcon aria-hidden className="size-4" />
      </span>
      <h2 className="mt-3 text-sm font-semibold">{title}</h2>
      <p className="text-muted-foreground mx-auto mt-1 max-w-lg text-sm leading-6">
        {description}
      </p>
      {action ? <div className="mt-3 flex justify-center">{action}</div> : null}
    </section>
  );
}

export function AdminInlineAlert({
  children,
  className,
  ...props
}: ComponentProps<"p">) {
  return (
    <p
      role="alert"
      className={cn(
        "border-destructive/20 bg-destructive/5 text-destructive flex items-start gap-2 rounded-lg border px-3 py-2.5 text-sm",
        className,
      )}
      {...props}
    >
      <AlertCircleIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
      <span>{children}</span>
    </p>
  );
}

export function AdminRecordList({ className, ...props }: ComponentProps<"ol">) {
  return (
    <ol
      data-slot="admin-record-list"
      className={cn(
        "border-border/80 bg-card divide-border divide-y overflow-hidden rounded-lg border",
        className,
      )}
      {...props}
    />
  );
}

export function AdminMobileRecordList({
  className,
  ...props
}: ComponentProps<"ol">) {
  return (
    <ol
      data-slot="admin-mobile-record-list"
      className={cn(
        "border-border/80 bg-card divide-border/70 divide-y overflow-hidden rounded-lg border xl:hidden",
        className,
      )}
      {...props}
    />
  );
}

export function AdminDataTable({
  className,
  containerClassName,
  ...props
}: ComponentProps<"table"> & { containerClassName?: string }) {
  return (
    <div
      data-slot="admin-data-table-container"
      className={cn(
        "border-border/80 bg-card max-w-full overflow-x-auto rounded-lg border",
        containerClassName,
      )}
    >
      <table
        data-slot="admin-data-table"
        className={cn("w-full border-collapse text-left text-sm", className)}
        {...props}
      />
    </div>
  );
}

export function AdminTechnicalValue({
  compact = false,
  copiedLabel,
  copyLabel,
  value,
}: {
  compact?: boolean;
  copiedLabel: string;
  copyLabel: string;
  value: string;
}) {
  const [copied, setCopied] = useState(false);

  return (
    <span
      data-slot="admin-technical-value"
      className="border-border/70 bg-muted/40 inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-md border py-1 pr-1 pl-2"
    >
      <code
        title={compact ? value : undefined}
        className={cn(
          "min-w-0 text-xs",
          compact ? "max-w-44 truncate" : "break-all",
        )}
      >
        {value}
      </code>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        className="size-7 shrink-0"
        aria-label={copied ? copiedLabel : copyLabel}
        title={copied ? copiedLabel : copyLabel}
        onClick={() => {
          if (!navigator.clipboard) return;
          void navigator.clipboard
            .writeText(value)
            .then(() => setCopied(true))
            .catch(() => setCopied(false));
        }}
      >
        {copied ? (
          <CheckIcon aria-hidden className="size-3.5" />
        ) : (
          <CopyIcon aria-hidden className="size-3.5" />
        )}
      </Button>
    </span>
  );
}
