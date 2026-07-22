"use client";

import { LogOutIcon, ShieldCheckIcon, UserRoundIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { User } from "@/core/auth/types";
import type { Project } from "@/core/projects/types";

import { ProjectDesktopNav, ProjectMobileNav } from "./project-nav";

function ProjectAccountMenu({
  accountEmail,
  systemRole,
  compact = false,
  onLogout,
}: {
  accountEmail: string;
  systemRole: User["system_role"];
  compact?: boolean;
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
  return (
    <div
      data-testid="project-shell"
      className="bg-background min-h-screen w-full overflow-x-hidden md:grid md:grid-cols-[16rem_minmax(0,1fr)]"
    >
      <ProjectDesktopNav
        project={project}
        footer={
          <ProjectAccountMenu
            accountEmail={accountEmail}
            systemRole={systemRole}
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
              onLogout={onLogout}
            />
          }
        />
        <div className="min-w-0">{children}</div>
      </div>
    </div>
  );
}
