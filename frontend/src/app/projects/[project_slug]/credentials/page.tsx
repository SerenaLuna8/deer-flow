import { ProjectCredentialPage } from "@/components/projects/credentials/project-credential-page";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectCredentialsPage({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "mcp.credentials.approve");
  return <ProjectCredentialPage />;
}
