import { ArrowLeftIcon, ArrowRightIcon } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function AdminPage({ className, ...props }: ComponentProps<"main">) {
  return (
    <main
      id="admin-main"
      data-slot="admin-page"
      className={cn(
        "mx-auto w-full max-w-[90rem] space-y-4 px-4 py-4 sm:px-5 sm:py-5 lg:px-6 lg:py-6",
        className,
      )}
      {...props}
    />
  );
}

export function AdminPageHeader({
  actions,
  className,
  description,
  eyebrow,
  meta,
  title,
}: {
  actions?: ReactNode;
  className?: string;
  description?: ReactNode;
  eyebrow?: ReactNode;
  meta?: ReactNode;
  title: ReactNode;
}) {
  return (
    <header
      data-slot="admin-page-header"
      className={cn(
        "border-border/70 flex flex-col gap-3 border-b pb-4 sm:flex-row sm:items-end sm:justify-between",
        className,
      )}
    >
      <div className="min-w-0">
        {eyebrow ? (
          <p className="text-muted-foreground mb-1.5 text-xs font-medium">
            {eyebrow}
          </p>
        ) : null}
        <h1 className="text-foreground text-xl font-semibold tracking-tight sm:text-2xl">
          {title}
        </h1>
        {description ? (
          <p className="text-muted-foreground mt-1.5 max-w-3xl text-sm leading-5">
            {description}
          </p>
        ) : null}
        {meta ? (
          <div className="text-muted-foreground mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs">
            {meta}
          </div>
        ) : null}
      </div>
      {actions ? (
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      ) : null}
    </header>
  );
}

export function AdminSection({
  actions,
  children,
  className,
  contentClassName,
  description,
  title,
  ...props
}: Omit<ComponentProps<"section">, "title"> & {
  actions?: ReactNode;
  contentClassName?: string;
  description?: ReactNode;
  title?: ReactNode;
}) {
  const hasHeader = [title, description, actions].some(Boolean);

  return (
    <section
      data-slot="admin-section"
      className={cn(
        "border-border/80 bg-card overflow-hidden rounded-lg border",
        className,
      )}
      {...props}
    >
      {hasHeader ? (
        <header className="border-border/70 flex flex-col gap-2 border-b px-4 py-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            {title ? (
              <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
            ) : null}
            {description ? (
              <p className="text-muted-foreground mt-1 text-sm leading-5">
                {description}
              </p>
            ) : null}
          </div>
          {actions ? (
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              {actions}
            </div>
          ) : null}
        </header>
      ) : null}
      <div className={cn("p-4", contentClassName)}>{children}</div>
    </section>
  );
}

export function AdminMetricGrid({
  className,
  ...props
}: ComponentProps<"div">) {
  return (
    <div
      data-slot="admin-metric-grid"
      className={cn(
        "bg-border grid gap-px overflow-hidden rounded-lg border sm:grid-cols-2 xl:grid-cols-4",
        className,
      )}
      {...props}
    />
  );
}

export function AdminMetric({
  className,
  detail,
  label,
  value,
}: {
  className?: string;
  detail?: ReactNode;
  label: ReactNode;
  value: ReactNode;
}) {
  return (
    <div
      data-slot="admin-metric"
      className={cn("bg-card min-w-0 px-4 py-3", className)}
    >
      <p className="text-muted-foreground text-xs font-medium">{label}</p>
      <p className="mt-1.5 text-xl font-semibold tracking-tight tabular-nums">
        {value}
      </p>
      {detail ? (
        <div className="text-muted-foreground mt-1.5 text-xs">{detail}</div>
      ) : null}
    </div>
  );
}

export interface AdminCursorState {
  cursor: string | null;
  history: Array<string | null>;
}

export const INITIAL_ADMIN_CURSOR_STATE: AdminCursorState = {
  cursor: null,
  history: [],
};

export function advanceAdminCursor(
  state: AdminCursorState,
  nextCursor: string | null,
): AdminCursorState {
  if (!nextCursor || nextCursor === state.cursor) return state;
  return {
    cursor: nextCursor,
    history: [...state.history, state.cursor],
  };
}

export function retreatAdminCursor(state: AdminCursorState): AdminCursorState {
  if (state.history.length === 0) return state;
  return {
    cursor: state.history[state.history.length - 1] ?? null,
    history: state.history.slice(0, -1),
  };
}

export function AdminCursorPagination({
  alwaysVisible = false,
  busy = false,
  className,
  itemCount,
  itemCountLabel,
  nextCursor,
  nextLabel,
  onNext,
  onPageSizeChange,
  onPrevious,
  pageLabel,
  pageSize,
  pageSizeLabel,
  pageSizeOptionLabel,
  pageSizeOptions,
  previousLabel,
  state,
}: {
  alwaysVisible?: boolean;
  busy?: boolean;
  className?: string;
  itemCount?: number;
  itemCountLabel?: (count: number) => string;
  nextCursor: string | null;
  nextLabel: string;
  onNext: () => void;
  onPageSizeChange?: (pageSize: number) => void;
  onPrevious: () => void;
  pageLabel: (page: number) => string;
  pageSize?: number;
  pageSizeLabel?: string;
  pageSizeOptionLabel?: (pageSize: number) => string;
  pageSizeOptions?: readonly number[];
  previousLabel: string;
  state: AdminCursorState;
}) {
  const hasPrevious = state.history.length > 0;
  const hasNext = Boolean(nextCursor && nextCursor !== state.cursor);
  const hasPageSize =
    pageSize !== undefined &&
    pageSizeOptions !== undefined &&
    pageSizeOptions.length > 0 &&
    onPageSizeChange !== undefined &&
    pageSizeLabel !== undefined &&
    pageSizeOptionLabel !== undefined;
  if (!alwaysVisible && !hasPrevious && !hasNext && !hasPageSize) return null;

  return (
    <nav
      data-slot="admin-cursor-pagination"
      aria-label={pageLabel(state.history.length + 1)}
      className={cn(
        "border-border/70 bg-card flex flex-wrap items-center justify-between gap-3 rounded-lg border px-3 py-2",
        className,
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-3">
        {hasPageSize ? (
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground text-xs font-medium">
              {pageSizeLabel}
            </span>
            <select
              aria-label={pageSizeLabel}
              value={pageSize}
              disabled={busy}
              onChange={(event) =>
                onPageSizeChange(Number(event.target.value))
              }
              className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-8 rounded-md border px-2 text-xs outline-none focus-visible:ring-[3px]"
            >
              {pageSizeOptions.map((option) => (
                <option key={option} value={option}>
                  {pageSizeOptionLabel(option)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {itemCount !== undefined && itemCountLabel ? (
          <span className="text-muted-foreground text-xs tabular-nums">
            {itemCountLabel(itemCount)}
          </span>
        ) : null}
      </div>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={!hasPrevious || busy}
          onClick={onPrevious}
        >
          <ArrowLeftIcon aria-hidden className="size-3.5" />
          {previousLabel}
        </Button>
        <span
          aria-current="page"
          className="text-muted-foreground min-w-16 text-center text-xs font-medium tabular-nums"
        >
          {pageLabel(state.history.length + 1)}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={!hasNext || busy}
          onClick={onNext}
        >
          {nextLabel}
          <ArrowRightIcon aria-hidden className="size-3.5" />
        </Button>
      </div>
    </nav>
  );
}
