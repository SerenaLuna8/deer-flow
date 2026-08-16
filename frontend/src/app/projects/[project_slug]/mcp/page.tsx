import { ProjectMcpPage as ProjectMcpAssetPage } from "@/components/projects/assets/project-mcp-page";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectMcpPage({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, [
    "shared_assets.edit",
    "shared_assets.manage_bindings",
  ]);
  return <ProjectMcpAssetPage />;
}
