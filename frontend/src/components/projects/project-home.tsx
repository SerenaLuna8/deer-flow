import {
  ArrowRightIcon,
  BotIcon,
  PlugZapIcon,
  SparklesIcon,
} from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import {
  canOpenProjectCapabilitiesWorkspace,
  canReadProjectAgents,
} from "@/core/projects/capabilities";
import type { Project } from "@/core/projects/types";

import { ProjectHeader } from "./project-header";

const assets = [
  {
    key: "agent",
    label: "共享 Agent",
    description: "选择、发布或启用项目可执行 Agent",
    path: "agents",
    countKey: "agent_count",
    icon: BotIcon,
  },
  {
    key: "skill",
    label: "共享 Skill",
    description: "维护项目成员共同使用的能力快照",
    path: "skills",
    countKey: "skill_count",
    icon: SparklesIcon,
  },
  {
    key: "mcp",
    label: "共享 MCP",
    description: "配置项目内可复用的工具连接定义",
    path: "mcp",
    countKey: "mcp_count",
    icon: PlugZapIcon,
  },
] as const;

export function ProjectHome({
  project,
  tokenUsageSection,
  usageDimensionsSection,
}: {
  project: Project;
  tokenUsageSection?: ReactNode;
  usageDimensionsSection?: ReactNode;
}) {
  const base = `/projects/${encodeURIComponent(project.slug)}`;
  const canManageCapabilities = canOpenProjectCapabilitiesWorkspace(
    project.capabilities,
  );
  const visibleAssets = assets.filter(({ key }) =>
    key === "agent"
      ? canReadProjectAgents(project.capabilities)
      : canManageCapabilities,
  );
  return (
    <div data-testid="project-home" className="bg-background min-h-screen">
      <ProjectHeader project={project} />
      <main className="mx-auto max-w-6xl space-y-8 px-4 py-8 sm:px-6 lg:px-8">
        {tokenUsageSection}
        {usageDimensionsSection}
        {visibleAssets.length > 0 && (
          <section>
            <div className="mb-4">
              <h2 className="text-xl font-semibold">共享资产</h2>
              <p className="text-muted-foreground mt-1 text-sm">
                查看当前项目可用的 Agent、Skill 和 MCP。
              </p>
            </div>
            <div className="grid gap-4 sm:grid-cols-3">
              {visibleAssets.map(
                ({ key, label, description, path, countKey, icon: Icon }) => (
                  <Link
                    key={key}
                    href={`${base}/${path}`}
                    className="border-border/70 bg-card hover:border-primary/40 hover:bg-accent/30 focus-visible:ring-ring group rounded-xl border p-5 transition-colors focus-visible:ring-2 focus-visible:outline-none"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <Icon className="text-primary" size={22} />
                      <span className="text-muted-foreground flex items-center gap-1.5 text-xs">
                        <strong className="text-foreground text-2xl leading-none font-semibold tabular-nums">
                          {project[countKey]}
                        </strong>
                        个可用
                        <ArrowRightIcon
                          aria-hidden
                          className="group-hover:text-primary size-4 transition-colors"
                        />
                      </span>
                    </div>
                    <span className="mt-5 block font-medium">{label}</span>
                    <span className="text-muted-foreground mt-2 block text-sm">
                      {description}
                    </span>
                  </Link>
                ),
              )}
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
