import { SkillBuilderStart } from "@/components/projects/skills/skill-builder-start";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectSkillBuilderStartPage({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "shared_assets.edit");
  return <SkillBuilderStart />;
}
