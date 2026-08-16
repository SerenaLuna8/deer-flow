import { ProjectSkillsPage as ProjectSkillsAssetPage } from "@/components/projects/assets/project-skills-page";
import { requireServerProjectCapability } from "@/core/projects/server-capability";
import { assetIdSchema } from "@/core/shared-assets";

export default async function ProjectSkillsPage({
  params,
  searchParams,
}: {
  params: Promise<{ project_slug: string }>;
  searchParams: Promise<{ skill_id?: string | string[] }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, [
    "shared_assets.edit",
    "shared_assets.manage_bindings",
  ]);
  const { skill_id: skillId } = await searchParams;
  const selectedAsset = assetIdSchema.safeParse(skillId);
  return (
    <ProjectSkillsAssetPage
      selectedAssetId={selectedAsset.success ? selectedAsset.data : null}
    />
  );
}
