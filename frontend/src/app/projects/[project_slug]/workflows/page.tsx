import { notFound } from "next/navigation";

import { isStaticWebsiteOnly } from "@/core/static-mode";

export default async function ProjectWorkflowsRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  if (isStaticWebsiteOnly()) notFound();
  const { project_slug: slug } = await params;
  const { requireServerProjectCapability } =
    await import("@/core/projects/server-capability");
  await requireServerProjectCapability(slug, "workflow.read");
  const { WorkflowDefinitionsRouteClient } =
    await import("@/components/projects/workflows/definitions/workflow-definitions-route-client");
  return <WorkflowDefinitionsRouteClient />;
}
