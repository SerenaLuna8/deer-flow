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
      description="选择项目可用的 Agent，查看角色设定、依赖与版本，并决定系统 Agent 是否在当前项目启用。"
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
