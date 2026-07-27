import { ProjectAgentsPage as ProjectAgentsAssetPage } from "@/components/projects/assets/project-agents-page";
import {
  isProjectStartChatIntent,
  projectStartChatIntentId,
} from "@/core/private-work/start-chat-intent";
import { assetIdSchema } from "@/core/shared-assets";

export default async function ProjectAgentsPage({
  searchParams,
}: {
  searchParams: Promise<{
    intent?: string | string[];
    intent_id?: string | string[];
    agent_id?: string | string[];
  }>;
}) {
  const {
    intent,
    intent_id: intentId,
    agent_id: agentId,
  } = await searchParams;
  const selectedAsset = assetIdSchema.safeParse(agentId);
  return (
    <ProjectAgentsAssetPage
      startChatIntent={isProjectStartChatIntent(intent)}
      startChatIntentId={projectStartChatIntentId(intentId)}
      selectedAssetId={selectedAsset.success ? selectedAsset.data : null}
    />
  );
}
