import { notFound } from "next/navigation";
import { z } from "zod";

import { isStaticWebsiteOnly } from "@/core/static-mode";

const workflowDefinitionIdSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u);

export default async function ProjectWorkflowDefinitionRoute({
  params,
}: {
  params: Promise<{ project_slug: string; workflow_id: string }>;
}) {
  if (isStaticWebsiteOnly()) notFound();
  const { project_slug: slug, workflow_id: workflowId } = await params;
  const { requireServerProjectCapability } =
    await import("@/core/projects/server-capability");
  await requireServerProjectCapability(slug, "workflow.read");
  const parsedWorkflowId = workflowDefinitionIdSchema.safeParse(workflowId);
  if (!parsedWorkflowId.success) notFound();
  const { WorkflowDefinitionRouteClient } =
    await import("@/components/projects/workflows/definitions/detail/workflow-definition-route-client");
  return <WorkflowDefinitionRouteClient workflowId={parsedWorkflowId.data} />;
}
