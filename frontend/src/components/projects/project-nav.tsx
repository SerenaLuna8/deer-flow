"use client";

import {
  ArrowLeftIcon,
  BotIcon,
  BrainCircuitIcon,
  CableIcon,
  FolderKanbanIcon,
  KeyRoundIcon,
  MessagesSquareIcon,
  MenuIcon,
  NetworkIcon,
  SettingsIcon,
  SparklesIcon,
  UsersIcon,
} from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import {
  projectPrivateWorkEntryEnabled,
  useProjectPrivateWorkReadiness,
} from "@/core/private-work/readiness";
import { PROJECT_PRIVATE_WORKSPACE } from "@/core/projects/features";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { cn } from "@/lib/utils";

type ProjectNavigationItem = {
  href: string;
  icon: typeof FolderKanbanIcon;
  label: string;
};

function canViewSettings(project: Project): boolean {
  return project.capabilities.some(
    (capability) =>
      capability === "project.lifecycle.manage" ||
      capability === "project.update",
  );
}

export function projectNavigationItems(
  project: Project,
  privateWorkReady = false,
  privateWorkFeatureEnabled: boolean = PROJECT_PRIVATE_WORKSPACE,
): ProjectNavigationItem[] {
  const base = `/projects/${encodeURIComponent(project.slug)}`;
  const items: ProjectNavigationItem[] = [
    { href: base, icon: FolderKanbanIcon, label: "项目概览" },
    { href: `${base}/members`, icon: UsersIcon, label: "成员与邀请" },
  ];
  if (
    projectPrivateWorkEntryEnabled(
      privateWorkFeatureEnabled,
      project.capabilities.includes("private_work.read_own"),
      privateWorkReady ? "ready" : undefined,
    )
  ) {
    items.push(
      {
        href: `${base}/chats`,
        icon: MessagesSquareIcon,
        label: "Chats",
      },
      {
        href: `${base}/memory`,
        icon: BrainCircuitIcon,
        label: "Memory",
      },
      {
        href: `${base}/connections`,
        icon: CableIcon,
        label: "Connections",
      },
    );
  }
  if (project.capabilities.includes("shared_assets.read")) {
    items.push(
      { href: `${base}/agents`, icon: BotIcon, label: "Agent" },
      { href: `${base}/skills`, icon: SparklesIcon, label: "Skill" },
      { href: `${base}/mcp`, icon: NetworkIcon, label: "MCP" },
      { href: `${base}/credentials`, icon: KeyRoundIcon, label: "Credential" },
    );
  }
  if (canViewSettings(project)) {
    items.push({
      href: `${base}/settings`,
      icon: SettingsIcon,
      label: "项目设置",
    });
  }
  return items;
}

function ProjectIdentity({ project }: { project: Project }) {
  return (
    <div className="flex min-w-0 items-center gap-3">
      <div className="bg-primary/10 text-primary flex size-10 shrink-0 items-center justify-center rounded-xl">
        {project.icon === "folder" ? (
          <FolderKanbanIcon aria-hidden className="size-5" />
        ) : (
          <span aria-hidden className="text-lg">
            {project.icon}
          </span>
        )}
      </div>
      <div className="min-w-0">
        <p className="truncate text-sm font-semibold">{project.display_name}</p>
        <div className="mt-1 flex min-w-0 items-center gap-2">
          <span className="text-muted-foreground truncate font-mono text-xs">
            {project.slug}
          </span>
          <Badge variant="secondary" className="shrink-0 text-[10px]">
            {project.role}
          </Badge>
        </div>
      </div>
    </div>
  );
}

function ProjectNavigationLinks({
  project,
  mobile = false,
}: {
  project: Project;
  mobile?: boolean;
}) {
  const canReadPrivateWork = project.capabilities.includes(
    "private_work.read_own",
  );
  const readiness = useProjectPrivateWorkReadiness(
    canReadPrivateWork && !isStaticWebsiteOnly(),
  );
  const links = projectNavigationItems(
    project,
    readiness.data?.status === "ready",
  ).map(({ href, icon: Icon, label }) => {
    const link = (
      <Link
        href={href}
        className="hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
      >
        <Icon aria-hidden className="text-muted-foreground size-4" />
        {label}
      </Link>
    );
    return mobile ? (
      <SheetClose key={href} asChild>
        {link}
      </SheetClose>
    ) : (
      <Link
        key={href}
        href={href}
        className="hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
      >
        <Icon aria-hidden className="text-muted-foreground size-4" />
        {label}
      </Link>
    );
  });
  const workspaceLink = (
    <Link
      href="/workspace"
      className="text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
    >
      <ArrowLeftIcon aria-hidden className="size-4" />
      返回工作空间
    </Link>
  );
  return (
    <nav className="flex flex-col gap-1">
      {links}
      {mobile ? (
        <SheetClose asChild>{workspaceLink}</SheetClose>
      ) : (
        workspaceLink
      )}
    </nav>
  );
}

export function ProjectDesktopNav({
  project,
  footer,
  className,
}: {
  project: Project;
  footer: React.ReactNode;
  className?: string;
}) {
  return (
    <aside
      className={cn(
        "border-border/70 bg-card sticky top-0 hidden h-screen w-64 flex-col self-start border-r md:flex",
        className,
      )}
    >
      <div className="border-border/70 border-b p-4">
        <ProjectIdentity project={project} />
      </div>
      <div className="min-h-0 flex-1 p-3">
        <ProjectNavigationLinks project={project} />
      </div>
      <div className="border-border/70 border-t p-3">{footer}</div>
    </aside>
  );
}

export function ProjectMobileNav({
  project,
  account,
}: {
  project: Project;
  account: React.ReactNode;
}) {
  return (
    <header className="border-border/70 bg-background/95 sticky top-0 z-30 flex min-w-0 items-center gap-2 border-b px-3 py-2 backdrop-blur md:hidden">
      <Sheet>
        <SheetTrigger asChild>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            aria-label="打开项目导航"
          >
            <MenuIcon aria-hidden className="size-5" />
          </Button>
        </SheetTrigger>
        <SheetContent side="left" className="w-[min(20rem,85vw)] p-0">
          <SheetHeader className="border-border/70 border-b p-4 text-left">
            <SheetTitle className="sr-only">项目导航</SheetTitle>
            <ProjectIdentity project={project} />
            <SheetDescription className="sr-only">
              项目页面导航
            </SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            <ProjectNavigationLinks project={project} mobile />
          </div>
        </SheetContent>
      </Sheet>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-semibold">{project.display_name}</p>
        <p className="text-muted-foreground truncate font-mono text-xs">
          {project.slug}
        </p>
      </div>
      {account}
    </header>
  );
}
