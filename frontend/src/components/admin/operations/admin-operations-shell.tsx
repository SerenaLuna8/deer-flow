"use client";

import {
  ActivityIcon,
  ArrowLeftIcon,
  BotIcon,
  BoxesIcon,
  BriefcaseBusinessIcon,
  ClipboardListIcon,
  FolderKanbanIcon,
  LogOutIcon,
  MenuIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  SettingsIcon,
  ShieldCheckIcon,
  XIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useRef, useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
  systemSettings: string;
  settings: string;
}

interface NavigationGroupLabels {
  operations: string;
  governance: string;
}

const DEFAULT_NAVIGATION_LABELS: NavigationLabels = {
  label: "Platform administration navigation",
  overview: "Overview",
  projects: "Projects",
  jobs: "Jobs",
  audit: "Logs",
  assets: "Assets",
  systemSettings: "System settings",
  settings: "Model settings",
};

export const ADMIN_NAVIGATION_EXPANDED_STORAGE_KEY =
  "deer-flow:admin-navigation-expanded";
const ADMIN_NAVIGATION_EXPANDED_EVENT =
  "deer-flow:admin-navigation-expanded-change";
let volatileAdminNavigationExpanded = false;

export function parseAdminNavigationExpanded(value: string | null): boolean {
  return value === "true";
}

export function adminDesktopSidebarLayout(expanded: boolean) {
  return expanded
    ? {
        contentPadding: "lg:pl-60",
        railWidth: "w-60",
      }
    : {
        contentPadding: "lg:pl-16",
        railWidth: "w-16",
      };
}

function getAdminNavigationExpandedSnapshot(): boolean {
  try {
    const stored = window.localStorage.getItem(
      ADMIN_NAVIGATION_EXPANDED_STORAGE_KEY,
    );
    return stored === null
      ? volatileAdminNavigationExpanded
      : parseAdminNavigationExpanded(stored);
  } catch {
    return volatileAdminNavigationExpanded;
  }
}

function getAdminNavigationExpandedServerSnapshot(): boolean {
  return false;
}

function subscribeAdminNavigationExpanded(onChange: () => void) {
  const handleStorage = (event: StorageEvent) => {
    if (event.key !== ADMIN_NAVIGATION_EXPANDED_STORAGE_KEY) return;
    volatileAdminNavigationExpanded = parseAdminNavigationExpanded(
      event.newValue,
    );
    onChange();
  };
  window.addEventListener("storage", handleStorage);
  window.addEventListener(ADMIN_NAVIGATION_EXPANDED_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", handleStorage);
    window.removeEventListener(ADMIN_NAVIGATION_EXPANDED_EVENT, onChange);
  };
}

function setAdminNavigationExpanded(expanded: boolean) {
  volatileAdminNavigationExpanded = expanded;
  try {
    window.localStorage.setItem(
      ADMIN_NAVIGATION_EXPANDED_STORAGE_KEY,
      String(expanded),
    );
  } catch {
    // The in-memory state still keeps the control usable when storage is denied.
  }
  window.dispatchEvent(new Event(ADMIN_NAVIGATION_EXPANDED_EVENT));
}

export function AdminOperationsNavigation({
  className,
  compact = false,
  groupLabels = {
    operations: "Operations and governance",
    governance: "Platform configuration",
  },
  idPrefix = "admin-navigation",
  mobile = false,
  pathname,
  labels = DEFAULT_NAVIGATION_LABELS,
}: {
  className?: string;
  compact?: boolean;
  groupLabels?: NavigationGroupLabels;
  idPrefix?: string;
  mobile?: boolean;
  pathname: string;
  labels?: NavigationLabels;
}) {
  const navigationGroups = [
    {
      id: "operations",
      label: groupLabels.operations,
      items: [
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
      ],
    },
    {
      id: "governance",
      label: groupLabels.governance,
      items: [
        {
          href: "/admin/assets",
          label: labels.assets,
          icon: BoxesIcon,
        },
        {
          href: "/admin/settings/system",
          label: labels.systemSettings,
          icon: SettingsIcon,
        },
        {
          href: "/admin/settings/models",
          label: labels.settings,
          icon: BotIcon,
        },
      ],
    },
  ] as const;

  return (
    <nav
      aria-label={labels.label}
      className={cn(
        "grid content-start overflow-x-hidden",
        compact ? "gap-2" : "gap-4",
        className,
      )}
    >
      {navigationGroups.map((group, groupIndex) => (
        <div
          key={group.id}
          role="group"
          aria-labelledby={`${idPrefix}-${group.id}`}
          data-navigation-group={group.id}
          className={cn(
            "grid",
            compact ? "justify-items-center gap-1" : "gap-1.5",
            groupIndex > 0 &&
              (compact
                ? "border-sidebar-border border-t pt-2"
                : "border-sidebar-border border-t pt-3"),
          )}
        >
          <p
            id={`${idPrefix}-${group.id}`}
            data-navigation-heading={group.id}
            className={cn(
              "text-muted-foreground text-[0.6875rem] font-semibold tracking-wide",
              compact ? "sr-only" : "px-3 pb-0.5",
            )}
          >
            {group.label}
          </p>
          {group.items.map(({ href, label, icon: Icon }) => {
            const active =
              pathname === href ||
              pathname.startsWith(`${href}/`) ||
              (href === "/admin/settings/models" &&
                pathname === "/admin/settings");
            const link = (
              <Link
                href={href}
                aria-label={compact ? label : undefined}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "focus-visible:ring-sidebar-ring relative flex min-h-10 items-center rounded-md text-sm font-medium transition-colors before:pointer-events-none before:absolute before:rounded-full focus-visible:ring-2 focus-visible:outline-none",
                  compact
                    ? "size-10 justify-center p-0 before:inset-y-2 before:left-1 before:w-0.5"
                    : "gap-2.5 px-3 py-2 before:inset-y-2 before:left-0 before:w-0.5",
                  active
                    ? "bg-blue-50 text-blue-600 before:bg-blue-600 dark:bg-blue-500/15 dark:text-blue-300 dark:before:bg-blue-300"
                    : "text-muted-foreground before:bg-transparent hover:bg-blue-50 dark:hover:bg-blue-500/15",
                )}
              >
                <Icon
                  aria-hidden
                  className={cn(
                    "size-4",
                    active
                      ? "text-blue-600 dark:text-blue-300"
                      : "text-muted-foreground",
                  )}
                />
                <span className={compact ? "sr-only" : "truncate"}>
                  {label}
                </span>
              </Link>
            );

            if (mobile) {
              return (
                <DialogClose key={href} asChild>
                  {link}
                </DialogClose>
              );
            }
            return compact ? (
              <Tooltip key={href}>
                <TooltipTrigger asChild>{link}</TooltipTrigger>
                <TooltipContent side="right" align="center" sideOffset={10}>
                  {label}
                </TooltipContent>
              </Tooltip>
            ) : (
              <div key={href}>{link}</div>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

function AdminSignOut({
  compact = false,
  label,
  onSignOut,
}: {
  compact?: boolean;
  label: string;
  onSignOut: () => void;
}) {
  return (
    <div className={cn("min-w-0", compact && "flex justify-center")}>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className={cn(
          "text-muted-foreground hover:text-foreground",
          compact ? "size-10 justify-center p-0" : "w-full justify-start",
        )}
        aria-label={label}
        title={compact ? label : undefined}
        onClick={onSignOut}
      >
        <LogOutIcon aria-hidden className="size-4" />
        <span className={compact ? "sr-only" : undefined}>{label}</span>
      </Button>
    </div>
  );
}

export function AdminWorkspaceLink({
  compact = false,
  label,
  mobile = false,
  testId,
}: {
  compact?: boolean;
  label: string;
  mobile?: boolean;
  testId: string;
}) {
  const link = (
    <Link
      data-testid={testId}
      href="/workspace"
      aria-label={compact ? label : undefined}
      title={compact ? label : undefined}
      className={cn(
        "text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-sidebar-ring flex min-h-9 items-center rounded-md text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:outline-none",
        compact ? "size-10 justify-center p-0" : "w-full gap-2 px-3 py-2",
      )}
    >
      <ArrowLeftIcon aria-hidden className="size-4 shrink-0" />
      <span className={compact ? "sr-only" : "truncate"}>{label}</span>
    </Link>
  );

  if (mobile) {
    return <DialogClose asChild>{link}</DialogClose>;
  }
  return compact ? (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right" align="center" sideOffset={10}>
        {label}
      </TooltipContent>
    </Tooltip>
  ) : (
    link
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
  const localLabels = t.adminOperations.ui;
  const desktopNavigationExpanded = useSyncExternalStore(
    subscribeAdminNavigationExpanded,
    getAdminNavigationExpandedSnapshot,
    getAdminNavigationExpandedServerSnapshot,
  );
  const desktopLayout = adminDesktopSidebarLayout(desktopNavigationExpanded);
  const desktopNavigationToggleLabel = desktopNavigationExpanded
    ? localLabels.collapseNavigation
    : localLabels.expandNavigation;
  const mobileCloseButtonRef = useRef<HTMLButtonElement>(null);
  const currentLabel =
    pathname === "/admin/projects" || pathname.startsWith("/admin/projects/")
      ? t.adminOperations.navigation.projects
      : pathname === "/admin/jobs" || pathname.startsWith("/admin/jobs/")
        ? t.adminOperations.navigation.jobs
        : pathname === "/admin/audit" || pathname.startsWith("/admin/audit/")
          ? t.adminOperations.navigation.audit
          : pathname === "/admin/assets" ||
              pathname.startsWith("/admin/assets/")
            ? t.adminOperations.navigation.assets
            : pathname === "/admin/settings/system" ||
                pathname.startsWith("/admin/settings/system/")
              ? t.adminOperations.navigation.systemSettings
              : pathname === "/admin/settings" ||
                  pathname === "/admin/settings/models" ||
                  pathname.startsWith("/admin/settings/models/")
                ? t.adminOperations.navigation.settings
                : t.adminOperations.navigation.overview;

  return (
    <div
      data-testid="admin-operations-shell"
      className="bg-muted/20 min-h-screen w-full overflow-x-clip"
    >
      <a
        href="#admin-main"
        className="bg-background text-foreground focus-visible:ring-ring fixed top-2 left-2 z-[60] -translate-y-16 rounded-md border px-3 py-2 text-sm font-medium shadow-sm transition-transform focus:translate-y-0 focus-visible:ring-2 focus-visible:outline-none"
      >
        {localLabels.skipToContent}
      </a>
      <aside
        id="admin-desktop-navigation"
        data-testid="admin-desktop-rail"
        data-expanded={String(desktopNavigationExpanded)}
        className={cn(
          "border-sidebar-border bg-sidebar text-sidebar-foreground fixed inset-y-0 left-0 z-40 hidden max-w-full flex-col overflow-x-hidden border-r transition-[width] duration-200 ease-out motion-reduce:transition-none lg:flex",
          desktopLayout.railWidth,
        )}
      >
        <div
          className={cn(
            "border-sidebar-border flex shrink-0 items-center border-b",
            desktopNavigationExpanded
              ? "h-14 justify-between gap-2 px-3"
              : "h-auto flex-col justify-center gap-1 px-2 py-2",
          )}
        >
          <Link
            href="/admin/operations"
            aria-label={t.adminOperations.shellTitle}
            title={t.adminOperations.shellTitle}
            className={cn(
              "focus-visible:ring-sidebar-ring flex min-w-0 items-center rounded-lg focus-visible:ring-2 focus-visible:outline-none",
              desktopNavigationExpanded
                ? "min-w-0 flex-1 gap-2.5 px-1 py-1.5"
                : "size-10 justify-center",
            )}
          >
            <ShieldCheckIcon
              aria-hidden
              className="text-selection size-5 shrink-0"
            />
            {desktopNavigationExpanded ? (
              <span className="block truncate text-sm font-semibold">
                {t.adminOperations.shellTitle}
              </span>
            ) : null}
          </Link>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                data-testid="admin-desktop-navigation-toggle"
                type="button"
                variant="ghost"
                size="icon"
                className="text-muted-foreground hover:text-foreground size-9 shrink-0"
                aria-controls="admin-desktop-navigation"
                aria-expanded={desktopNavigationExpanded}
                aria-label={desktopNavigationToggleLabel}
                title={desktopNavigationToggleLabel}
                onClick={() =>
                  setAdminNavigationExpanded(!desktopNavigationExpanded)
                }
              >
                {desktopNavigationExpanded ? (
                  <PanelLeftCloseIcon aria-hidden className="size-4" />
                ) : (
                  <PanelLeftOpenIcon aria-hidden className="size-4" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="right" align="center" sideOffset={10}>
              {desktopNavigationToggleLabel}
            </TooltipContent>
          </Tooltip>
        </div>
        <div className="border-sidebar-border shrink-0 border-b px-2.5 py-2">
          <AdminWorkspaceLink
            compact={!desktopNavigationExpanded}
            label={localLabels.backToWorkspace}
            testId="admin-desktop-workspace-link"
          />
        </div>
        <AdminOperationsNavigation
          compact={!desktopNavigationExpanded}
          pathname={pathname}
          labels={t.adminOperations.navigation}
          groupLabels={localLabels.navigationGroups}
          className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto px-2.5 py-4"
        />
        <div className="border-sidebar-border border-t px-2.5 py-3">
          {user?.email && desktopNavigationExpanded ? (
            <p className="text-muted-foreground mb-2 truncate px-2 text-xs">
              {user.email}
            </p>
          ) : null}
          <AdminSignOut
            compact={!desktopNavigationExpanded}
            label={t.adminOperations.signOut}
            onSignOut={() => void logout()}
          />
        </div>
      </aside>

      <div
        data-testid="admin-shell-content"
        className={cn(
          "min-h-screen min-w-0 transition-[padding-left] duration-200 ease-out motion-reduce:transition-none",
          desktopLayout.contentPadding,
        )}
      >
        <header
          data-testid="admin-shell-topbar"
          className="border-border/70 bg-background/95 sticky top-0 z-30 flex h-14 items-center justify-between border-b px-4 backdrop-blur sm:px-5 lg:px-6"
        >
          <div className="flex min-w-0 items-center gap-2.5">
            <Link
              href="/admin/operations"
              className="focus-visible:ring-ring flex min-w-0 items-center gap-2 rounded-md focus-visible:ring-2 focus-visible:outline-none lg:hidden"
            >
              <span className="bg-selection-subtle text-selection flex size-8 shrink-0 items-center justify-center rounded-md">
                <ShieldCheckIcon aria-hidden className="size-4" />
              </span>
              <span className="truncate text-sm font-semibold">
                {t.adminOperations.shellTitle}
              </span>
            </Link>
            <div className="hidden min-w-0 items-center gap-2 lg:flex">
              <p className="truncate text-sm font-semibold">
                {t.adminOperations.shellTitle}
              </p>
              <span aria-hidden className="text-border">
                /
              </span>
              <p className="text-muted-foreground truncate text-sm">
                {currentLabel}
              </p>
            </div>
          </div>
          <Dialog>
            <DialogTrigger asChild>
              <Button
                data-testid="admin-mobile-navigation-trigger"
                type="button"
                variant="outline"
                size="icon"
                className="lg:hidden"
                aria-label={t.adminOperations.navigation.label}
              >
                <MenuIcon aria-hidden className="size-4" />
              </Button>
            </DialogTrigger>
            <DialogContent
              showCloseButton={false}
              onOpenAutoFocus={(event) => {
                event.preventDefault();
                mobileCloseButtonRef.current?.focus();
              }}
              className="border-sidebar-border bg-sidebar text-sidebar-foreground top-0 left-0 h-dvh w-[min(19rem,88vw)] max-w-none translate-x-0 translate-y-0 gap-0 rounded-none border-y-0 border-l-0 p-0 sm:max-w-none"
            >
              <div className="border-sidebar-border flex items-start justify-between gap-3 border-b px-4 py-4 text-left">
                <div className="min-w-0">
                  <DialogTitle className="truncate text-base">
                    {t.adminOperations.shellTitle}
                  </DialogTitle>
                  <DialogDescription className="mt-1">
                    {t.adminOperations.shellDescription}
                  </DialogDescription>
                </div>
                <DialogClose asChild>
                  <Button
                    ref={mobileCloseButtonRef}
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label={localLabels.close}
                    title={localLabels.close}
                  >
                    <XIcon aria-hidden className="size-4" />
                  </Button>
                </DialogClose>
              </div>
              <AdminOperationsNavigation
                mobile
                idPrefix="admin-mobile-navigation"
                pathname={pathname}
                labels={t.adminOperations.navigation}
                groupLabels={localLabels.navigationGroups}
                className="min-h-0 flex-1 overflow-y-auto px-2.5 py-4"
              />
              <div className="border-sidebar-border border-t px-3 py-3">
                {user?.email ? (
                  <p className="text-muted-foreground mb-2 truncate px-2 text-xs">
                    {user.email}
                  </p>
                ) : null}
                <div className="grid gap-1">
                  <AdminWorkspaceLink
                    label={localLabels.backToWorkspace}
                    mobile
                    testId="admin-mobile-workspace-link"
                  />
                  <AdminSignOut
                    label={t.adminOperations.signOut}
                    onSignOut={() => void logout()}
                  />
                </div>
              </div>
            </DialogContent>
          </Dialog>
          <div className="hidden min-w-0 lg:block">
            <p className="text-muted-foreground max-w-[18rem] truncate text-xs">
              {user?.email}
            </p>
          </div>
        </header>
        {children}
      </div>
    </div>
  );
}
