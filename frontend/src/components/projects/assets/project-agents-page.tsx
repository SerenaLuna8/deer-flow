"use client";

import { ProjectAgentStartContinuation } from "@/components/projects/private-work/project-agent-start-continuation";

import { AgentAssetDetail } from "./agent-asset-detail";
import { ProjectAssetPageShell } from "./project-asset-page-shell";

export function ProjectAgentsPage({
  startChatIntent = false,
  startChatIntentId = null,
}: {
  startChatIntent?: boolean;
  startChatIntentId?: string | null;
}) {
  return (
    <ProjectAssetPageShell
      kind="agents"
      title="Agent"
      description="创建和维护当前项目自建 Agent 的角色设定、依赖与版本。系统默认 Main 不在此列表中展示。"
      renderLead={({ project, data }) => (
        <ProjectAgentStartContinuation
          project={project}
          catalog={data}
          requested={startChatIntent}
          intentId={startChatIntentId}
        />
      )}
      renderVersion={(version) =>
        "agent_id" in version ? (
          <AgentAssetDetail version={version} />
        ) : (
          <p role="alert" className="text-destructive text-sm">
            Agent 版本数据无效。
          </p>
        )
      }
    />
  );
}
