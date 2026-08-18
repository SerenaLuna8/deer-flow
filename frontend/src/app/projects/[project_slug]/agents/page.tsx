import { ProjectAgentsPage as ProjectAgentsAssetPage } from "@/components/projects/assets/project-agents-page";
import {
  isProjectStartChatIntent,
  projectStartChatIntentId,
} from "@/core/private-work/start-chat-intent";
import { requireServerProjectCapability } from "@/core/projects/server-capability";
import { assetIdSchema } from "@/core/shared-assets";

export default async function ProjectAgentsPage({
  params,
  searchParams,
}: {
  params: Promise<{ project_slug: string }>;
  searchParams: Promise<{
    intent?: string | string[];
    intent_id?: string | string[];
    agent_id?: string | string[];
  }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "shared_assets.read");
  const { intent, intent_id: intentId, agent_id: agentId } = await searchParams;
  const selectedAsset = assetIdSchema.safeParse(agentId);
  return (
    <ProjectAgentsAssetPage
      startChatIntent={isProjectStartChatIntent(intent)}
      startChatIntentId={projectStartChatIntentId(intentId)}
      selectedAssetId={selectedAsset.success ? selectedAsset.data : null}
    />
  );
}
