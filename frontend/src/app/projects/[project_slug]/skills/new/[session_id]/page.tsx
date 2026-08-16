import { SkillBuilderWorkspace } from "@/components/projects/skills/skill-builder-workspace";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectSkillBuilderSessionPage({
  params,
}: {
  params: Promise<{ project_slug: string; session_id: string }>;
}) {
  const { project_slug: slug, session_id: sessionId } = await params;
  await requireServerProjectCapability(slug, "shared_assets.edit");
  return <SkillBuilderWorkspace sessionId={sessionId} />;
}
