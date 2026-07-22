"use client";

import {
  ArrowLeftIcon,
  BotIcon,
  BrainCircuitIcon,
  CalendarClockIcon,
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
import { usePathname } from "next/navigation";

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
  section: ProjectNavigationSection;
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
      section: "work",
    },
  ];
  if (
    !staticWebsiteOnly &&
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
        label: "会话",
        section: "work",
      },
      {
        href: `${base}/memory`,
        icon: BrainCircuitIcon,
        label: "Memory",
        section: "work",
      },
      {
        href: `${base}/connections`,
        icon: CableIcon,
        label: "Connections",
        section: "work",
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
      {
        href: `${base}/credentials`,
        icon: KeyRoundIcon,
        label: "Credential",
        section: "capabilities",
      },
    );
  }
  items.push({
    href: `${base}/members`,
    icon: UsersIcon,
    label: "成员与邀请",
    section: "management",
  });
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
      aria-label="DeerFlow 工作空间"
      className="focus-visible:ring-ring inline-flex items-baseline gap-2 rounded-md focus-visible:ring-2 focus-visible:outline-none"
    >
      <span className="text-primary font-serif text-xl leading-none">
        DeerFlow
      </span>
      <span className="text-muted-foreground text-xs">项目空间</span>
    </Link>
  );
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
      privateWorkReady={privateWorkReady}
      automationReady={automationReady}
      staticWebsiteOnly={staticWebsiteOnly}
    />
  );
}

function ProjectNavigationLinksContent({
  project,
  mobile,
  privateWorkReady,
  automationReady,
  staticWebsiteOnly,
}: {
  project: Project;
  mobile: boolean;
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
  const renderLink = ({
    href,
    icon: Icon,
    i18nKey,
    label,
  }: ProjectNavigationItem) => {
    const visibleLabel = i18nKey ? t.project[i18nKey] : label;
    const active = isProjectNavigationItemActive(href, pathname);
    const link = (
      <Link
        key={href}
        href={href}
        aria-current={active ? "page" : undefined}
        className={cn(
          "focus-visible:ring-ring flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:outline-none",
          active
            ? "bg-foreground text-background shadow-sm"
            : "hover:bg-accent hover:text-accent-foreground",
        )}
      >
        <Icon
          aria-hidden
          className={cn(
            "size-4",
            active ? "text-background" : "text-muted-foreground",
          )}
        />
        {visibleLabel}
      </Link>
    );
    return mobile ? (
      <SheetClose key={href} asChild>
        {link}
      </SheetClose>
    ) : (
      link
    );
  };
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
    <nav aria-label="项目导航" className="flex flex-col gap-5">
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
              className="text-muted-foreground mb-1 px-3 text-[11px] font-semibold tracking-[0.14em]"
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
          workspaceLink
        )}
      </div>
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
      <div className="border-border/70 border-b px-4 py-5">
        <ProjectBrand />
        <div className="mt-5">
          <ProjectIdentity project={project} />
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
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
        <p className="truncate text-sm font-semibold">{project.display_name}</p>
        <p className="text-muted-foreground truncate font-mono text-xs">
          {project.slug}
        </p>
      </div>
      {account}
    </header>
  );
}
