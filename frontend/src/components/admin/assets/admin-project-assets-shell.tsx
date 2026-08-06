"use client";

import {
  ArrowLeftIcon,
  BotIcon,
  KeyRoundIcon,
  LockKeyholeIcon,
  NetworkIcon,
  SparklesIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

const NAVIGATION = [
  { segment: "agents", labelKey: "agent", icon: BotIcon },
  { segment: "skills", labelKey: "skill", icon: SparklesIcon },
  { segment: "mcp", labelKey: "mcp", icon: NetworkIcon },
  { segment: "credentials", labelKey: "credential", icon: KeyRoundIcon },
] as const;

export function AdminProjectAssetsShell({
  projectId,
  children,
}: {
  projectId: string;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const base = `/admin/projects/${projectId}/assets`;
  const { t } = useI18n();

  return (
    <section
      data-testid="admin-project-assets-shell"
      aria-label={t.adminAssets.shell.projectAria}
      className="bg-background min-w-0 overflow-x-clip"
    >
      <div className="border-border bg-card border-b">
        <div className="mx-auto max-w-[90rem] px-4 sm:px-5 lg:px-6">
          <div
            data-testid="admin-project-assets-context"
            className="grid min-w-0 gap-3 border-b py-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center"
          >
            <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:gap-4">
              <Link
                href="/admin/projects"
                className="text-muted-foreground hover:text-foreground focus-visible:ring-ring inline-flex h-8 w-fit shrink-0 items-center gap-1 text-sm focus-visible:rounded-sm focus-visible:ring-2 focus-visible:outline-none"
              >
                <ArrowLeftIcon aria-hidden className="size-4" />
                {t.adminAssets.shell.backToProjects}
              </Link>
              <span
                aria-hidden
                className="bg-border mt-2 hidden h-4 w-px sm:block"
              />
              <div className="min-w-0">
                <p className="text-sm font-semibold">
                  {t.adminAssets.shell.projectGovernance}
                </p>
                <p className="text-muted-foreground mt-0.5 max-w-3xl text-xs">
                  <LockKeyholeIcon
                    aria-hidden
                    className="mr-1 inline size-3.5"
                  />
                  {t.adminAssets.shell.projectBoundary}
                </p>
              </div>
            </div>
            <div
              className="border-border bg-muted/45 min-w-0 rounded-md border px-3 py-2 text-xs sm:whitespace-nowrap"
              title={projectId}
            >
              <span className="text-muted-foreground mr-2 font-medium">
                {t.adminAssets.shell.projectId}
              </span>
              <span className="font-mono [overflow-wrap:anywhere] sm:[overflow-wrap:normal]">
                {projectId}
              </span>
            </div>
          </div>
          <nav
            aria-label={t.adminAssets.navigation.projectLabel}
            data-variant="line"
            className="grid min-w-0 grid-cols-4 items-center gap-1 sm:flex sm:grid-cols-4 sm:gap-5"
          >
            {NAVIGATION.map(({ segment, labelKey, icon: Icon }) => {
              const href = `${base}/${segment}`;
              const active = pathname === href;
              return (
                <Link
                  key={segment}
                  href={href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "focus-visible:ring-ring -mb-px flex min-w-0 items-center justify-center gap-1.5 border-b-2 border-transparent px-1 py-3 text-sm font-medium focus-visible:rounded-sm focus-visible:ring-2 focus-visible:outline-none sm:justify-start sm:gap-2",
                    active
                      ? "border-primary text-foreground"
                      : "text-muted-foreground hover:border-border hover:text-foreground",
                  )}
                >
                  <Icon aria-hidden className="size-4 shrink-0" />
                  <span className="truncate">
                    {t.adminAssets.navigation[labelKey]}
                  </span>
                </Link>
              );
            })}
          </nav>
        </div>
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}
