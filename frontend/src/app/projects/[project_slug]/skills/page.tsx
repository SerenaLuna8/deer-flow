import { z } from "zod";

import { ProjectSkillsPage as ProjectSkillsAssetPage } from "@/components/projects/assets/project-skills-page";
import { requireServerProjectCapability } from "@/core/projects/server-capability";
import { assetIdSchema } from "@/core/shared-assets";

const configureSecretsSchema = z.literal("1");

export default async function ProjectSkillsPage({
  params,
  searchParams,
}: {
  params: Promise<{ project_slug: string }>;
  searchParams: Promise<{
    skill_id?: string | string[];
    skill_version_id?: string | string[];
    configure_secrets?: string | string[];
  }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "shared_assets.read");
  const {
    skill_id: skillId,
    skill_version_id: skillVersionId,
    configure_secrets: configureSecrets,
  } = await searchParams;
  const selectedAsset = assetIdSchema.safeParse(skillId);
  const selectedVersion = assetIdSchema.safeParse(skillVersionId);
  const exactSelectionValid = selectedAsset.success && selectedVersion.success;
  return (
    <ProjectSkillsAssetPage
      selectedAssetId={selectedAsset.success ? selectedAsset.data : null}
      selectedVersionId={exactSelectionValid ? selectedVersion.data : null}
      focusSelectedSkillSecrets={
        exactSelectionValid &&
        configureSecretsSchema.safeParse(configureSecrets).success
      }
    />
  );
}
