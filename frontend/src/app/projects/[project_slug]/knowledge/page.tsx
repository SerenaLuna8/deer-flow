import { ProjectKnowledgePage } from "@/components/projects/knowledge/project-knowledge-page";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectKnowledgeRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, ["shared_assets.read"]);
  return <ProjectKnowledgePage />;
}
