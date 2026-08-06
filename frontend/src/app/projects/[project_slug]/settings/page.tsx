import { ProjectGeneralSettings } from "@/components/projects/settings/project-general-settings";
import { ProjectLifecyclePanel } from "@/components/projects/settings/project-lifecycle-panel";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectSettingsRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(
    slug,
    ["project.update", "project.lifecycle.manage"],
    { match: "any" },
  );
  return (
    <div className="space-y-8">
      <ProjectGeneralSettings />
      <ProjectLifecyclePanel />
    </div>
  );
}
