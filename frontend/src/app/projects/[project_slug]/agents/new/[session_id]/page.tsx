import { AgentBuilderWorkspace } from "@/components/projects/agents/agent-builder-workspace";

export default async function ProjectAgentBuilderSessionPage({
  params,
}: {
  params: Promise<{ session_id: string }>;
}) {
  const { session_id: sessionId } = await params;
  return <AgentBuilderWorkspace sessionId={sessionId} />;
}
