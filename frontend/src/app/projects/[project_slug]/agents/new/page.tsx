import { AgentBuilderStart } from "@/components/projects/agents/agent-builder-start";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function NewProjectAgentPage({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "shared_assets.edit");
  return <AgentBuilderStart />;
}
