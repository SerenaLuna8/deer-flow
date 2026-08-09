import { notFound, redirect } from "next/navigation";

import { requireServerProjectCapability } from "@/core/projects/server-capability";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export default async function ProjectSettingsAuditRedirectRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  if (isStaticWebsiteOnly()) notFound();
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "project.audit.read");
  redirect(`/projects/${encodeURIComponent(slug)}/audit`);
}
