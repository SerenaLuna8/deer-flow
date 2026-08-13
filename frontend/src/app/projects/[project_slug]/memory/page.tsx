import { notFound } from "next/navigation";

import { ProjectMemoryPage } from "@/components/projects/private-work/project-memory-page";
import { requireServerProjectCapability } from "@/core/projects/server-capability";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export default async function ProjectMemoryRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  if (isStaticWebsiteOnly()) notFound();
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "private_work.read_own");
  return <ProjectMemoryPage />;
}
