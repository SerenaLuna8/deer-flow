"use client";

import { GaugeIcon, ScrollTextIcon, Settings2Icon } from "lucide-react";
import Link from "next/link";
import { notFound, usePathname } from "next/navigation";

import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { cn } from "@/lib/utils";

import { useCurrentProject } from "../project-context";

type ProjectSettingsNavigationItem = {
  href: string;
  label: string;
  description: string;
  icon: typeof Settings2Icon;
};

export function projectSettingsNavigationItems(
  project: Project,
  staticWebsiteOnly: boolean,
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
  if (
    !staticWebsiteOnly &&
    project.capabilities.includes("project.usage.read")
  ) {
    items.push({
      href: `${base}/usage`,
      label: "用量与限额",
      description: "当前使用量与有效上限",
      icon: GaugeIcon,
    });
  }
  if (
    !staticWebsiteOnly &&
    project.capabilities.includes("project.audit.read")
  ) {
    items.push({
      href: `${base}/audit`,
      label: "审计日志",
      description: "项目中的关键操作记录",
      icon: ScrollTextIcon,
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
  const items = projectSettingsNavigationItems(project, isStaticWebsiteOnly());

  if (items.length === 0) notFound();

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <header className="mb-8 max-w-3xl">
        <p className="text-primary mb-2 text-sm font-medium">
          {project.display_name} · 治理
        </p>
        <h1 className="text-3xl font-semibold tracking-tight">项目设置</h1>
        <p className="text-muted-foreground mt-2">
          管理项目资料、资源用量和关键操作记录。
        </p>
      </header>

      <div className="grid items-start gap-8 lg:grid-cols-[14rem_minmax(0,1fr)]">
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
                    ? "bg-foreground text-background"
                    : "hover:bg-accent text-foreground",
                )}
              >
                <Icon
                  aria-hidden
                  className={cn(
                    "mt-0.5 size-4 shrink-0",
                    active ? "text-background" : "text-muted-foreground",
                  )}
                />
                <span className="min-w-0">
                  <span className="block text-sm font-medium">{label}</span>
                  <span
                    className={cn(
                      "mt-0.5 block text-xs leading-5",
                      active ? "text-background/70" : "text-muted-foreground",
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
