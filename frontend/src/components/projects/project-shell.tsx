"use client";

import { LogOutIcon, UserRoundIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Project } from "@/core/projects/types";

import { ProjectDesktopNav, ProjectMobileNav } from "./project-nav";

function ProjectAccountMenu({
  accountEmail,
  compact = false,
  onLogout,
}: {
  accountEmail: string;
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
  onLogout,
  children,
}: {
  project: Project;
  accountEmail: string;
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
          <ProjectAccountMenu accountEmail={accountEmail} onLogout={onLogout} />
        }
      />
      <div className="min-w-0">
        <ProjectMobileNav
          project={project}
          account={
            <ProjectAccountMenu
              accountEmail={accountEmail}
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
