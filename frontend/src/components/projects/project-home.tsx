import { BotIcon, PlugZapIcon, SparklesIcon } from "lucide-react";

import { PROJECT_PRIVATE_WORKSPACE } from "@/core/projects/features";
import type { Project } from "@/core/projects/types";

import { RecentPrivateWork } from "./private-work/recent-private-work";
import { ProjectHeader } from "./project-header";
import { ProjectPrivateWorkCta } from "./project-private-work-cta";

const assets = [
  { key: "agent", label: "共享 Agent", icon: BotIcon },
  { key: "skill", label: "共享 Skill", icon: SparklesIcon },
  { key: "mcp", label: "共享 MCP", icon: PlugZapIcon },
] as const;

export function ProjectHome({ project }: { project: Project }) {
  const counts = {
    agent: project.agent_count,
    skill: project.skill_count,
    mcp: project.mcp_count,
  };
  return (
    <div data-testid="project-home" className="bg-background min-h-screen">
      <ProjectHeader project={project} />
      <main className="mx-auto max-w-6xl space-y-8 px-4 py-8 sm:px-6 lg:px-8">
        <section className="border-primary/20 bg-primary/5 rounded-2xl border p-5">
          <h2 className="font-semibold">项目隐私边界</h2>
          <p className="text-muted-foreground mt-2 text-sm">
            对话和记忆私有，Agent、Skill 和 MCP 共享。
          </p>
        </section>
        <section>
          <div className="mb-4">
            <h2 className="text-xl font-semibold">共享资产</h2>
            <p className="text-muted-foreground mt-1 text-sm">
              资产管理将在后续里程碑开放。
            </p>
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            {assets.map(({ key, label, icon: Icon }) => (
              <div
                key={key}
                className="border-border/70 bg-card rounded-xl border p-5"
              >
                <Icon className="text-primary mb-4" size={22} />
                <div className="flex items-end justify-between gap-3">
                  <span className="font-medium">{label}</span>
                  <span className="text-2xl font-semibold">{counts[key]}</span>
                </div>
                <span className="text-muted-foreground mt-3 block text-xs">
                  后续里程碑
                </span>
              </div>
            ))}
          </div>
        </section>
        {PROJECT_PRIVATE_WORKSPACE &&
          project.capabilities.includes("private_work.read_own") && (
            <RecentPrivateWork project={project} />
          )}
        <ProjectPrivateWorkCta project={project} />
      </main>
    </div>
  );
}
