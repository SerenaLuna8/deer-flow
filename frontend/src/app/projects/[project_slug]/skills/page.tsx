import { ProjectSkillsPage as ProjectSkillsAssetPage } from "@/components/projects/assets/project-skills-page";
import { assetIdSchema } from "@/core/shared-assets";

export default async function ProjectSkillsPage({
  searchParams,
}: {
  searchParams: Promise<{ skill_id?: string | string[] }>;
}) {
  const { skill_id: skillId } = await searchParams;
  const selectedAsset = assetIdSchema.safeParse(skillId);
  return (
    <ProjectSkillsAssetPage
      selectedAssetId={selectedAsset.success ? selectedAsset.data : null}
    />
  );
}
