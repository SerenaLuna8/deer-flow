"use client";

import { Settings2Icon, UsersIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import type { Project } from "@/core/projects/types";
import { cn } from "@/lib/utils";

import { ProjectAccessDenied } from "../project-access-denied";
import { useCurrentProject } from "../project-context";
import { ProjectPageHeader } from "../project-page-header";

type ProjectSettingsNavigationItem = {
  href: string;
  label: string;
  description: string;
  icon: typeof Settings2Icon;
};

export function projectSettingsNavigationItems(
  project: Project,
): ProjectSettingsNavigationItem[] {
  const base = `/projects/${encodeURIComponent(project.slug)}/settings`;
  const items: ProjectSettingsNavigationItem[] = [];
  if (
    project.capabilities.includes("project.update") ||
    project.capabilities.includes("project.lifecycle.manage")
  ) {
    items.push({
      href: base,
      label: "常规设置",
      description: "项目资料与生命周期",
      icon: Settings2Icon,
    });
  }
  if (project.capabilities.includes("project.members.manage")) {
    items.push({
      href: `${base}/members`,
      label: "项目成员",
      description: "成员角色与邀请",
      icon: UsersIcon,
    });
  }
  return items;
}

export function ProjectSettingsShell({
  children,
}: {
  children: React.ReactNode;
}) {
  const project = useCurrentProject();
  const pathname = usePathname();
  const items = projectSettingsNavigationItems(project);

  if (items.length === 0) {
    return <ProjectAccessDenied projectSlug={project.slug} area="项目设置" />;
  }

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <ProjectPageHeader
        className="mb-6"
        eyebrow={`${project.display_name} · 治理`}
        title="项目设置"
        description="管理项目资料、成员和生命周期。"
      />

      <div className="grid items-start gap-6 lg:grid-cols-[14rem_minmax(0,1fr)]">
        <nav
          aria-label="项目设置"
          className="border-border/70 bg-card grid gap-1 rounded-2xl border p-2 lg:sticky lg:top-6"
        >
          {items.map(({ href, label, description, icon: Icon }) => {
            const active =
              pathname === href ||
              (href.endsWith("/settings")
                ? false
                : pathname.startsWith(`${href}/`));
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "focus-visible:ring-ring flex items-start gap-3 rounded-xl px-3 py-3 transition-colors focus-visible:ring-2 focus-visible:outline-none",
                  active
                    ? "bg-blue-50 text-blue-600 dark:bg-blue-500/15 dark:text-blue-300"
                    : "text-foreground hover:bg-blue-50 dark:hover:bg-blue-500/15",
                )}
              >
                <Icon
                  aria-hidden
                  className={cn(
                    "mt-0.5 size-4 shrink-0",
                    active
                      ? "text-blue-600 dark:text-blue-300"
                      : "text-muted-foreground",
                  )}
                />
                <span className="min-w-0">
                  <span className="block text-sm font-medium">{label}</span>
                  <span
                    className={cn(
                      "mt-0.5 block text-xs leading-5",
                      active
                        ? "text-blue-600/75 dark:text-blue-300/75"
                        : "text-muted-foreground",
                    )}
                  >
                    {description}
                  </span>
                </span>
              </Link>
            );
          })}
        </nav>

        <div className="min-w-0">{children}</div>
      </div>
    </main>
  );
}
