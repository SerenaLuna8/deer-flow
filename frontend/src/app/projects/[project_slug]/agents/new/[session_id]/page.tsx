import { AgentBuilderWorkspace } from "@/components/projects/agents/agent-builder-workspace";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectAgentBuilderSessionPage({
  params,
}: {
  params: Promise<{ project_slug: string; session_id: string }>;
}) {
  const { project_slug: slug, session_id: sessionId } = await params;
  await requireServerProjectCapability(slug, "shared_assets.read");
  return <AgentBuilderWorkspace sessionId={sessionId} />;
}
