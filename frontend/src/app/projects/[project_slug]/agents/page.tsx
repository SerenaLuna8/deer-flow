import { ProjectAgentsPage as ProjectAgentsAssetPage } from "@/components/projects/assets/project-agents-page";
import {
  isProjectStartChatIntent,
  projectStartChatIntentId,
} from "@/core/private-work/start-chat-intent";

export default async function ProjectAgentsPage({
  searchParams,
}: {
  searchParams: Promise<{
    intent?: string | string[];
    intent_id?: string | string[];
  }>;
}) {
  const { intent, intent_id: intentId } = await searchParams;
  return (
    <ProjectAgentsAssetPage
      startChatIntent={isProjectStartChatIntent(intent)}
      startChatIntentId={projectStartChatIntentId(intentId)}
    />
  );
}
