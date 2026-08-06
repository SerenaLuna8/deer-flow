import { SkillBuilderWorkspace } from "@/components/projects/skills/skill-builder-workspace";

export default async function ProjectSkillBuilderSessionPage({
  params,
}: {
  params: Promise<{ session_id: string }>;
}) {
  const { session_id: sessionId } = await params;
  return <SkillBuilderWorkspace sessionId={sessionId} />;
}
