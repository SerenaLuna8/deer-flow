"use client";

import {
  ArrowLeftIcon,
  BotIcon,
  BrainCircuitIcon,
  CableIcon,
  CalendarClockIcon,
  FolderKanbanIcon,
  MessagesSquareIcon,
  MenuIcon,
  NetworkIcon,
  PanelLeftCloseIcon,
  ScrollTextIcon,
  SettingsIcon,
  SparklesIcon,
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
  canOpenProjectCapabilitiesWorkspace,
  canReadProjectAgents,
} from "@/core/projects/capabilities";
import {
  PROJECT_AUTOMATION,
  PROJECT_PRIVATE_WORKSPACE,
  projectAutomationEntryEnabled,
} from "@/core/projects/features";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { cn } from "@/lib/utils";

type ProjectNavigationItem = {
  id:
    | "overview"
    | "conversations"
    | "automations"
    | "agents"
    | "skills"
    | "mcp"
    | "memory"
    | "connections"
    | "audit"
    | "settings";
  href: string;
  icon: typeof FolderKanbanIcon;
  section: ProjectNavigationSection | null;
};

type ProjectNavigationSection = "capabilities" | "management" | "work";

const PROJECT_NAVIGATION_SECTIONS: ProjectNavigationSection[] = [
  "work",
  "capabilities",
  "management",
];

function canViewSettings(project: Project): boolean {
  return project.capabilities.some(
    (capability) =>
      capability === "project.lifecycle.manage" ||
      capability === "project.update" ||
      capability === "project.members.manage",
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
      id: "overview",
      href: base,
      icon: FolderKanbanIcon,
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
    items.push({
      id: "conversations",
      href: `${base}/chats`,
      icon: MessagesSquareIcon,
      section: "work",
    });
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
      id: "automations",
      href: `${base}/automations`,
      icon: CalendarClockIcon,
      section: "work",
    });
  }
  const capabilityWorkspaceVisible = canOpenProjectCapabilitiesWorkspace(
    project.capabilities,
  );
  if (canReadProjectAgents(project.capabilities)) {
    items.push({
      id: "agents",
      href: `${base}/agents`,
      icon: BotIcon,
      section: "capabilities",
    });
  }
  if (capabilityWorkspaceVisible) {
    items.push(
      {
        id: "skills",
        href: `${base}/skills`,
        icon: SparklesIcon,
        section: "capabilities",
      },
      {
        id: "mcp",
        href: `${base}/mcp`,
        icon: NetworkIcon,
        section: "capabilities",
      },
    );
  }
  if (privateWorkEnabled) {
    items.push({
      id: "memory",
      href: `${base}/memory`,
      icon: BrainCircuitIcon,
      section: "work",
    });
  }
  if (
    !staticWebsiteOnly &&
    privateWorkFeatureEnabled &&
    project.capabilities.includes("project.channels.manage")
  ) {
    items.push({
      id: "connections",
      href: `${base}/connections`,
      icon: CableIcon,
      section: "management",
    });
  }
  if (
    !staticWebsiteOnly &&
    project.capabilities.includes("project.audit.read")
  ) {
    items.push({
      id: "audit",
      href: `${base}/audit`,
      icon: ScrollTextIcon,
      section: "management",
    });
  }
  if (canViewSettings(project)) {
    const settingsBase = `${base}/settings`;
    const canOpenGeneral =
      project.capabilities.includes("project.update") ||
      project.capabilities.includes("project.lifecycle.manage");
    items.push({
      id: "settings",
      href:
        canOpenGeneral ||
        !project.capabilities.includes("project.members.manage")
          ? settingsBase
          : `${settingsBase}/members`,
      icon: SettingsIcon,
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
  const { t } = useI18n();
  return (
    <Link
      href="/workspace"
      aria-label={t.project.navigation.workspaceAria}
      className="focus-visible:ring-ring inline-flex min-w-0 items-center gap-2 rounded-md focus-visible:ring-2 focus-visible:outline-none"
    >
      <span
        data-slot="project-brand-logo"
        aria-hidden
        className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-blue-50 dark:bg-blue-500/15"
      >
        <span className="size-5 bg-blue-600 mask-[url(/images/deer.svg)] mask-contain mask-center mask-no-repeat dark:bg-blue-300" />
      </span>
      <span className="text-primary truncate font-serif text-xl leading-none">
        Fluva
      </span>
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
  const renderLink = ({ id, href, icon: Icon }: ProjectNavigationItem) => {
    const visibleLabel = t.project.navigation[id];
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
      aria-label={collapsed ? t.project.navigation.backToWorkspace : undefined}
      title={collapsed ? t.project.navigation.backToWorkspace : undefined}
      className={cn(
        "text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring flex items-center rounded-lg text-sm font-medium focus-visible:ring-2 focus-visible:outline-none",
        collapsed ? "size-10 justify-center" : "gap-3 px-3 py-2",
      )}
    >
      <ArrowLeftIcon aria-hidden className="size-4" />
      {!collapsed && t.project.navigation.backToWorkspace}
    </Link>
  );
  const visibleWorkspaceLink = collapsed ? (
    <Tooltip>
      <TooltipTrigger asChild>{workspaceLink}</TooltipTrigger>
      <TooltipContent side="right" align="center">
        {t.project.navigation.backToWorkspace}
      </TooltipContent>
    </Tooltip>
  ) : (
    workspaceLink
  );
  return (
    <nav
      aria-label={t.project.navigation.label}
      className={cn("flex flex-col", collapsed ? "gap-2" : "gap-5")}
    >
      {standaloneLinks.length > 0 && (
        <div className="grid gap-1">{standaloneLinks.map(renderLink)}</div>
      )}
      {PROJECT_NAVIGATION_SECTIONS.map((section) => {
        const sectionLinks = links.filter((item) => item.section === section);
        if (sectionLinks.length === 0) return null;
        const sectionId = `project-${mobile ? "mobile" : "desktop"}-nav-${section}`;
        return (
          <section key={section} aria-labelledby={sectionId}>
            <p
              id={sectionId}
              className={cn(
                "text-muted-foreground mb-1 px-3 text-[11px] font-semibold tracking-[0.14em]",
                collapsed && "sr-only",
              )}
            >
              {t.project.navigation.sections[section]}
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
  compactFooter,
  collapsed = false,
  onCollapsedChange,
  className,
}: {
  project: Project;
  footer: React.ReactNode;
  compactFooter: React.ReactNode;
  collapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
  className?: string;
}) {
  const { t } = useI18n();
  return (
    <aside
      aria-label={t.project.navigation.menuLabel}
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
              aria-label={t.project.navigation.expand}
              title={t.project.navigation.expand}
              onClick={() => onCollapsedChange?.(false)}
            >
              <MenuIcon aria-hidden className="size-5" />
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            <ProjectNavigationLinks project={project} collapsed />
          </div>
          <div className="border-border/70 border-t p-2">{compactFooter}</div>
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
                aria-label={t.project.navigation.collapse}
                title={t.project.navigation.collapse}
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
  const { t } = useI18n();
  return (
    <header className="border-border/70 bg-background/95 sticky top-0 z-30 flex min-w-0 items-center gap-2 border-b px-3 py-2 backdrop-blur md:hidden">
      <Sheet>
        <SheetTrigger asChild>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            aria-label={t.project.navigation.open}
          >
            <MenuIcon aria-hidden className="size-5" />
          </Button>
        </SheetTrigger>
        <SheetContent
          side="left"
          className="bg-sidebar w-[min(20rem,85vw)] p-0"
        >
          <SheetHeader className="border-foreground/15 border-b p-4 text-left">
            <SheetTitle className="sr-only">
              {t.project.navigation.sheetTitle}
            </SheetTitle>
            <ProjectBrand />
            <div className="pt-3">
              <ProjectIdentity project={project} />
            </div>
            <SheetDescription className="sr-only">
              {t.project.navigation.sheetDescription}
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
