"use client";

import {
  LogOutIcon,
  SettingsIcon,
  ShieldCheckIcon,
  UserRoundIcon,
} from "lucide-react";
import Link from "next/link";
import {
  createContext,
  useContext,
  useMemo,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { SettingsDialog } from "@/components/workspace/settings";
import type { User } from "@/core/auth/types";
import type { Project } from "@/core/projects/types";
import { cn } from "@/lib/utils";

import { ProjectDesktopNav, ProjectMobileNav } from "./project-nav";

type ProjectDesktopNavigationState = {
  collapsed: boolean;
  setCollapsed: Dispatch<SetStateAction<boolean>>;
};

const ProjectDesktopNavigationContext =
  createContext<ProjectDesktopNavigationState | null>(null);

export function useProjectDesktopNavigation() {
  const context = useContext(ProjectDesktopNavigationContext);
  if (!context) {
    throw new Error(
      "useProjectDesktopNavigation must be used within ProjectShell",
    );
  }
  return context;
}

function ProjectAccountMenu({
  accountEmail,
  systemRole,
  compact = false,
  onOpenSettings,
  onLogout,
}: {
  accountEmail: string;
  systemRole: User["system_role"];
  compact?: boolean;
  onOpenSettings: () => void;
  onLogout: () => void | Promise<void>;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          className={compact ? "shrink-0 px-2" : "w-full justify-start"}
          aria-label="账户"
        >
          <UserRoundIcon aria-hidden className="size-4 shrink-0" />
          {!compact && <span className="truncate">{accountEmail}</span>}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="truncate">
          {accountEmail}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {systemRole === "system_admin" ? (
          <>
            <DropdownMenuItem asChild>
              <Link href="/admin/operations">
                <ShieldCheckIcon aria-hidden className="size-4" />
                平台管理
              </Link>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
          </>
        ) : null}
        <DropdownMenuItem onSelect={onOpenSettings}>
          <SettingsIcon aria-hidden className="size-4" />
          系统设置
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => void onLogout()}>
          <LogOutIcon aria-hidden className="size-4" />
          退出登录
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function ProjectShell({
  project,
  accountEmail,
  systemRole,
  onLogout,
  children,
}: {
  project: Project;
  accountEmail: string;
  systemRole: User["system_role"];
  onLogout: () => void | Promise<void>;
  children: React.ReactNode;
}) {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [desktopNavCollapsed, setDesktopNavCollapsed] = useState(false);
  const desktopNavigation = useMemo(
    () => ({
      collapsed: desktopNavCollapsed,
      setCollapsed: setDesktopNavCollapsed,
    }),
    [desktopNavCollapsed],
  );

  return (
    <ProjectDesktopNavigationContext.Provider value={desktopNavigation}>
      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        defaultSection="appearance"
      />
      <div
        data-testid="project-shell"
        className={cn(
          "bg-background min-h-screen w-full overflow-x-clip md:grid md:transition-[grid-template-columns] md:duration-200 md:ease-out",
          desktopNavCollapsed
            ? "md:grid-cols-[3.5rem_minmax(0,1fr)]"
            : "md:grid-cols-[15rem_minmax(0,1fr)]",
        )}
      >
        <ProjectDesktopNav
          project={project}
          collapsed={desktopNavCollapsed}
          onCollapsedChange={setDesktopNavCollapsed}
          footer={
            <ProjectAccountMenu
              accountEmail={accountEmail}
              systemRole={systemRole}
              onOpenSettings={() => setSettingsOpen(true)}
              onLogout={onLogout}
            />
          }
        />
        <div className="min-w-0">
          <ProjectMobileNav
            project={project}
            account={
              <ProjectAccountMenu
                accountEmail={accountEmail}
                systemRole={systemRole}
                compact
                onOpenSettings={() => setSettingsOpen(true)}
                onLogout={onLogout}
              />
            }
          />
          <div className="min-w-0">{children}</div>
        </div>
      </div>
    </ProjectDesktopNavigationContext.Provider>
  );
}
