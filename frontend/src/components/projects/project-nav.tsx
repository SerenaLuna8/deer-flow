"use client";

import {
  ArrowLeftIcon,
  BotIcon,
  BrainCircuitIcon,
  CableIcon,
  CalendarClockIcon,
  FolderKanbanIcon,
  KeyRoundIcon,
  MessagesSquareIcon,
  MenuIcon,
  NetworkIcon,
  PanelLeftCloseIcon,
  SettingsIcon,
  SparklesIcon,
  UsersIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

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
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useI18n } from "@/core/i18n/hooks";
import {
  projectPrivateWorkEntryEnabled,
  useProjectPrivateWorkReadiness,
} from "@/core/private-work/readiness";
import { useProjectAutomationReadiness } from "@/core/project-automations/readiness";
import {
  PROJECT_AUTOMATION,
  PROJECT_PRIVATE_WORKSPACE,
  projectAutomationEntryEnabled,
} from "@/core/projects/features";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { cn } from "@/lib/utils";

type ProjectNavigationItem = {
  href: string;
  icon: typeof FolderKanbanIcon;
  i18nKey?: "audit" | "automations" | "usage";
  label: string;
  section: ProjectNavigationSection | null;
};

type ProjectNavigationSection = "capabilities" | "management" | "work";

const PROJECT_NAVIGATION_SECTIONS: Array<{
  id: ProjectNavigationSection;
  label: string;
}> = [
  { id: "work", label: "工作" },
  { id: "capabilities", label: "能力" },
  { id: "management", label: "项目管理" },
];

function canViewSettings(project: Project): boolean {
  return project.capabilities.some(
    (capability) =>
      capability === "project.lifecycle.manage" ||
      capability === "project.update" ||
      capability === "project.usage.read" ||
      capability === "project.audit.read",
  );
}

export function projectNavigationItems(
  project: Project,
  privateWorkReady = false,
  privateWorkFeatureEnabled: boolean = PROJECT_PRIVATE_WORKSPACE,
  automationReady = false,
  automationFeatureEnabled: boolean = PROJECT_AUTOMATION,
  staticWebsiteOnly = false,
  _usageReady = false,
  _auditReady = false,
): ProjectNavigationItem[] {
  const base = `/projects/${encodeURIComponent(project.slug)}`;
  const items: ProjectNavigationItem[] = [
    {
      href: base,
      icon: FolderKanbanIcon,
      label: "项目概览",
      section: null,
    },
  ];
  const privateWorkEnabled =
    !staticWebsiteOnly &&
    projectPrivateWorkEntryEnabled(
      privateWorkFeatureEnabled,
      project.capabilities.includes("private_work.read_own"),
      privateWorkReady ? "ready" : undefined,
    );
  if (privateWorkEnabled) {
    items.push(
      {
        href: `${base}/chats`,
        icon: MessagesSquareIcon,
        label: "会话",
        section: "work",
      },
      {
        href: `${base}/connections`,
        icon: CableIcon,
        label: "渠道连接",
        section: "management",
      },
    );
  }
  if (
    projectAutomationEntryEnabled(
      automationFeatureEnabled,
      staticWebsiteOnly,
      project.capabilities.includes("private_work.read_own"),
      automationReady ? "ready" : undefined,
    )
  ) {
    items.push({
      href: `${base}/automations`,
      icon: CalendarClockIcon,
      i18nKey: "automations",
      label: "Automations",
      section: "work",
    });
  }
  if (project.capabilities.includes("shared_assets.read")) {
    items.push(
      {
        href: `${base}/agents`,
        icon: BotIcon,
        label: "Agent",
        section: "capabilities",
      },
      {
        href: `${base}/skills`,
        icon: SparklesIcon,
        label: "Skill",
        section: "capabilities",
      },
      {
        href: `${base}/mcp`,
        icon: NetworkIcon,
        label: "MCP",
        section: "capabilities",
      },
    );
  }
  if (privateWorkEnabled) {
    items.push({
      href: `${base}/memory`,
      icon: BrainCircuitIcon,
      label: "Memory",
      section: "capabilities",
    });
  }
  if (project.capabilities.includes("mcp.credentials.approve")) {
    items.push({
      href: `${base}/credentials`,
      icon: KeyRoundIcon,
      label: "项目凭证",
      section: "management",
    });
  }
  if (project.capabilities.includes("project.members.manage")) {
    items.push({
      href: `${base}/members`,
      icon: UsersIcon,
      label: "项目成员",
      section: "management",
    });
  }
  if (canViewSettings(project)) {
    items.push({
      href: `${base}/settings`,
      icon: SettingsIcon,
      label: "项目设置",
      section: "management",
    });
  }
  return items;
}

export function isProjectNavigationItemActive(
  href: string,
  pathname: string,
): boolean {
  const target = href.replace(/\/+$/u, "") || "/";
  const current = pathname.replace(/\/+$/u, "") || "/";
  const isProjectRoot = target.split("/").filter(Boolean).length === 2;
  return (
    current === target || (!isProjectRoot && current.startsWith(`${target}/`))
  );
}

function ProjectBrand() {
  return (
    <Link
      href="/workspace"
      aria-label="ActWeave 工作空间"
      className="focus-visible:ring-ring inline-flex items-baseline gap-2 rounded-md focus-visible:ring-2 focus-visible:outline-none"
    >
      <span className="text-primary font-serif text-xl leading-none">
        ActWeave
      </span>
      <span className="text-muted-foreground text-xs">项目空间</span>
    </Link>
  );
}

function ProjectIdentity({ project }: { project: Project }) {
  return (
    <div className="flex min-h-11 min-w-0 items-center gap-3">
      <div className="bg-primary/10 text-primary flex size-11 shrink-0 items-center justify-center rounded-xl">
        {project.icon === "folder" ? (
          <FolderKanbanIcon aria-hidden className="size-6" />
        ) : (
          <span aria-hidden className="text-xl">
            {project.icon}
          </span>
        )}
      </div>
      <p className="min-w-0 truncate text-base font-semibold">
        {project.display_name}
      </p>
    </div>
  );
}

function ProjectNavigationLinks({
  project,
  mobile = false,
  collapsed = false,
}: {
  project: Project;
  mobile?: boolean;
  collapsed?: boolean;
}) {
  const canReadPrivateWork = project.capabilities.includes(
    "private_work.read_own",
  );
  const staticWebsiteOnly = isStaticWebsiteOnly();
  const readiness = useProjectPrivateWorkReadiness(
    canReadPrivateWork && !staticWebsiteOnly,
  );
  const automationReadiness = useProjectAutomationReadiness(
    PROJECT_AUTOMATION && canReadPrivateWork && !staticWebsiteOnly,
  );
  const privateWorkReady = readiness.data?.status === "ready";
  const automationReady =
    automationReadiness.data?.status === "ready" &&
    automationReadiness.data.project_private_work_ready &&
    automationReadiness.data.schema_ready;
  return (
    <ProjectNavigationLinksContent
      project={project}
      mobile={mobile}
      collapsed={collapsed && !mobile}
      privateWorkReady={privateWorkReady}
      automationReady={automationReady}
      staticWebsiteOnly={staticWebsiteOnly}
    />
  );
}

function ProjectNavigationLinksContent({
  project,
  mobile,
  collapsed,
  privateWorkReady,
  automationReady,
  staticWebsiteOnly,
}: {
  project: Project;
  mobile: boolean;
  collapsed: boolean;
  privateWorkReady: boolean;
  automationReady: boolean;
  staticWebsiteOnly: boolean;
}) {
  const { t } = useI18n();
  const pathname = usePathname();
  const links = projectNavigationItems(
    project,
    privateWorkReady,
    PROJECT_PRIVATE_WORKSPACE,
    automationReady,
    PROJECT_AUTOMATION,
    staticWebsiteOnly,
  );
  const standaloneLinks = links.filter((item) => item.section === null);
  const renderLink = ({
    href,
    icon: Icon,
    i18nKey,
    label,
  }: ProjectNavigationItem) => {
    const visibleLabel = i18nKey ? t.project[i18nKey] : label;
    const active = isProjectNavigationItemActive(href, pathname);
    const iconOnly = collapsed && !mobile;
    const link = (
      <Link
        key={href}
        href={href}
        aria-label={iconOnly ? visibleLabel : undefined}
        aria-current={active ? "page" : undefined}
        title={iconOnly ? visibleLabel : undefined}
        className={cn(
          "focus-visible:ring-ring relative flex items-center rounded-lg text-sm font-medium transition-colors before:pointer-events-none before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-full focus-visible:ring-2 focus-visible:outline-none",
          iconOnly ? "size-10 justify-center" : "gap-3 px-3 py-2",
          active
            ? "bg-sidebar-accent text-sidebar-accent-foreground before:bg-selection"
            : "hover:bg-sidebar-accent hover:text-sidebar-accent-foreground before:bg-transparent",
        )}
      >
        <Icon
          aria-hidden
          className={cn(
            "size-4",
            active ? "text-foreground" : "text-muted-foreground",
          )}
        />
        {!iconOnly && visibleLabel}
      </Link>
    );
    if (mobile) {
      return (
        <SheetClose key={href} asChild>
          {link}
        </SheetClose>
      );
    }
    return iconOnly ? (
      <Tooltip key={href}>
        <TooltipTrigger asChild>{link}</TooltipTrigger>
        <TooltipContent side="right" align="center">
          {visibleLabel}
        </TooltipContent>
      </Tooltip>
    ) : (
      link
    );
  };
  const workspaceLink = (
    <Link
      href="/workspace"
      aria-label={collapsed ? "返回工作空间" : undefined}
      title={collapsed ? "返回工作空间" : undefined}
      className={cn(
        "text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring flex items-center rounded-lg text-sm font-medium focus-visible:ring-2 focus-visible:outline-none",
        collapsed ? "size-10 justify-center" : "gap-3 px-3 py-2",
      )}
    >
      <ArrowLeftIcon aria-hidden className="size-4" />
      {!collapsed && "返回工作空间"}
    </Link>
  );
  const visibleWorkspaceLink = collapsed ? (
    <Tooltip>
      <TooltipTrigger asChild>{workspaceLink}</TooltipTrigger>
      <TooltipContent side="right" align="center">
        返回工作空间
      </TooltipContent>
    </Tooltip>
  ) : (
    workspaceLink
  );
  return (
    <nav
      aria-label="项目导航"
      className={cn("flex flex-col", collapsed ? "gap-2" : "gap-5")}
    >
      {standaloneLinks.length > 0 && (
        <div className="grid gap-1">{standaloneLinks.map(renderLink)}</div>
      )}
      {PROJECT_NAVIGATION_SECTIONS.map((section) => {
        const sectionLinks = links.filter(
          (item) => item.section === section.id,
        );
        if (sectionLinks.length === 0) return null;
        const sectionId = `project-${mobile ? "mobile" : "desktop"}-nav-${section.id}`;
        return (
          <section key={section.id} aria-labelledby={sectionId}>
            <p
              id={sectionId}
              className={cn(
                "text-muted-foreground mb-1 px-3 text-[11px] font-semibold tracking-[0.14em]",
                collapsed && "sr-only",
              )}
            >
              {section.label}
            </p>
            <div className="grid gap-1">{sectionLinks.map(renderLink)}</div>
          </section>
        );
      })}
      <div className="border-border/70 border-t pt-3">
        {mobile ? (
          <SheetClose asChild>{workspaceLink}</SheetClose>
        ) : (
          visibleWorkspaceLink
        )}
      </div>
    </nav>
  );
}

export function ProjectDesktopNav({
  project,
  footer,
  collapsed = false,
  onCollapsedChange,
  className,
}: {
  project: Project;
  footer: React.ReactNode;
  collapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
  className?: string;
}) {
  return (
    <aside
      aria-label="项目菜单栏"
      data-state={collapsed ? "collapsed" : "expanded"}
      className={cn(
        "border-border/70 bg-sidebar sticky top-0 hidden h-screen min-w-0 flex-col self-start overflow-hidden border-r md:flex",
        className,
      )}
    >
      {collapsed ? (
        <>
          <div className="border-border/70 flex h-[4.75rem] items-center justify-center border-b">
            <Button
              type="button"
              size="icon"
              variant="ghost"
              aria-label="展开菜单栏"
              title="展开菜单栏"
              onClick={() => onCollapsedChange?.(false)}
            >
              <MenuIcon aria-hidden className="size-5" />
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            <ProjectNavigationLinks project={project} collapsed />
          </div>
        </>
      ) : (
        <>
          <div className="border-foreground/15 h-[4.75rem] border-b px-4">
            <div className="flex h-full items-center justify-between gap-2">
              <ProjectBrand />
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="-mr-1 size-8 shrink-0"
                aria-label="收起菜单栏"
                title="收起菜单栏"
                onClick={() => onCollapsedChange?.(true)}
              >
                <PanelLeftCloseIcon aria-hidden className="size-4" />
              </Button>
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            <ProjectNavigationLinks project={project} />
          </div>
          <div className="border-border/70 border-t p-3">{footer}</div>
        </>
      )}
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
        <SheetContent
          side="left"
          className="bg-sidebar w-[min(20rem,85vw)] p-0"
        >
          <SheetHeader className="border-foreground/15 border-b p-4 text-left">
            <SheetTitle className="sr-only">项目导航</SheetTitle>
            <ProjectBrand />
            <div className="pt-3">
              <ProjectIdentity project={project} />
            </div>
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
        <p className="truncate text-base font-semibold">
          {project.display_name}
        </p>
      </div>
      {account}
    </header>
  );
}
