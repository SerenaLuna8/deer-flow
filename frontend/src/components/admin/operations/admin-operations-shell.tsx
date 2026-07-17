"use client";

import {
  ActivityIcon,
  BoxesIcon,
  BriefcaseBusinessIcon,
  ClipboardListIcon,
  FolderKanbanIcon,
  LogOutIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

interface NavigationLabels {
  label: string;
  overview: string;
  projects: string;
  jobs: string;
  audit: string;
  assets: string;
}

const DEFAULT_NAVIGATION_LABELS: NavigationLabels = {
  label: "Platform operations navigation",
  overview: "Overview",
  projects: "Projects",
  jobs: "Jobs",
  audit: "Audit",
  assets: "Assets",
};

export function AdminOperationsNavigation({
  pathname,
  labels = DEFAULT_NAVIGATION_LABELS,
}: {
  pathname: string;
  labels?: NavigationLabels;
}) {
  const navigation = [
    {
      href: "/admin/operations",
      label: labels.overview,
      icon: ActivityIcon,
    },
    {
      href: "/admin/projects",
      label: labels.projects,
      icon: FolderKanbanIcon,
    },
    {
      href: "/admin/jobs",
      label: labels.jobs,
      icon: BriefcaseBusinessIcon,
    },
    {
      href: "/admin/audit",
      label: labels.audit,
      icon: ClipboardListIcon,
    },
    {
      href: "/admin/assets",
      label: labels.assets,
      icon: BoxesIcon,
    },
  ] as const;

  return (
    <nav aria-label={labels.label} className="flex gap-1 overflow-x-auto">
      {navigation.map(({ href, label, icon: Icon }) => {
        const active =
          pathname === href ||
          (href === "/admin/assets" && pathname.startsWith(`${href}/`));
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "focus-visible:ring-ring flex shrink-0 items-center gap-2 rounded-md px-3 py-2 text-sm font-medium focus-visible:ring-2 focus-visible:outline-none",
              active
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
            )}
          >
            <Icon aria-hidden className="size-4" />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}

export function AdminOperationsShell({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const { t } = useI18n();

  return (
    <div
      data-testid="admin-operations-shell"
      className="bg-background min-h-screen"
    >
      <header className="border-border/70 bg-background/95 sticky top-0 z-40 border-b backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-4 px-4 py-3 lg:px-6">
          <div className="mr-auto min-w-48">
            <p className="text-primary font-serif text-lg">
              {t.adminOperations.shellTitle}
            </p>
            <p className="text-muted-foreground text-xs">
              {t.adminOperations.shellDescription}
            </p>
          </div>
          <AdminOperationsNavigation
            pathname={pathname}
            labels={t.adminOperations.navigation}
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label={t.adminOperations.signOut}
            onClick={() => void logout()}
          >
            <LogOutIcon aria-hidden className="size-4" />
            <span className="hidden max-w-40 truncate xl:inline">
              {user?.email}
            </span>
          </Button>
        </div>
      </header>
      {children}
    </div>
  );
}
