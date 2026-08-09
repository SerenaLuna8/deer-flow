import { forbidden, notFound, redirect } from "next/navigation";

import { ProjectGeneralSettings } from "@/components/projects/settings/project-general-settings";
import { ProjectLifecyclePanel } from "@/components/projects/settings/project-lifecycle-panel";
import {
  lookupServerProjectBySlug,
  requireServerProjectCapability,
} from "@/core/projects/server-capability";

export default async function ProjectSettingsRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  const lookup = await lookupServerProjectBySlug(slug);
  if (lookup.status === "not_found") notFound();
  if (lookup.status === "ready") {
    const canOpenGeneral =
      lookup.project.capabilities.includes("project.update") ||
      lookup.project.capabilities.includes("project.lifecycle.manage");
    if (
      !canOpenGeneral &&
      lookup.project.capabilities.includes("project.members.manage")
    ) {
      redirect(`/projects/${encodeURIComponent(slug)}/settings/members`);
    }
    if (!canOpenGeneral) forbidden();
  } else {
    await requireServerProjectCapability(
      slug,
      ["project.update", "project.lifecycle.manage"],
      { match: "any" },
    );
  }
  return (
    <div className="space-y-8">
      <ProjectGeneralSettings />
      <ProjectLifecyclePanel />
    </div>
  );
}
