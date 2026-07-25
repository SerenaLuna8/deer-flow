import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function ProjectPageHeader({
  title,
  description,
  eyebrow,
  icon,
  actions,
  className,
}: {
  title: string;
  description: string;
  eyebrow?: string;
  icon?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header
      className={cn(
        "flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between",
        className,
      )}
    >
      <div className="min-w-0">
        {eyebrow ? (
          <p className="text-muted-foreground mb-1 text-xs font-medium">
            {eyebrow}
          </p>
        ) : null}
        <div className="flex min-w-0 items-center gap-2.5">
          {icon ? (
            <span className="bg-muted text-foreground flex size-9 shrink-0 items-center justify-center rounded-lg">
              {icon}
            </span>
          ) : null}
          <h1 className="truncate text-2xl font-semibold tracking-tight">
            {title}
          </h1>
        </div>
        <p className="text-muted-foreground mt-1 max-w-3xl text-sm leading-5">
          {description}
        </p>
      </div>
      {actions ? <div className="shrink-0">{actions}</div> : null}
    </header>
  );
}
