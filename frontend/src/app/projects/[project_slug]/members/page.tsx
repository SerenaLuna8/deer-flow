import { redirect } from "next/navigation";

import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectMembersRedirectRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "project.members.manage");
  redirect(`/projects/${encodeURIComponent(slug)}/settings/members`);
}
