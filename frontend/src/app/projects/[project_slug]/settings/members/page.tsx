import { ProjectMembersPage } from "@/components/projects/members/project-members-page";
import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectSettingsMembersRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "project.members.manage");
  return <ProjectMembersPage embedded />;
}
