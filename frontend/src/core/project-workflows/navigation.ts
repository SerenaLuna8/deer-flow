export type ProjectWorkflowControlPlaneReadiness = {
  status: "ready" | "unavailable";
  workflow_enabled: boolean;
  schema_ready: boolean;
};

export type ProjectWorkflowNavigationInput = {
  staticWebsiteOnly: boolean;
  canReadWorkflow: boolean;
  readiness: ProjectWorkflowControlPlaneReadiness | undefined;
};

export function projectWorkflowNavigationVisible({
  staticWebsiteOnly,
  canReadWorkflow,
  readiness,
}: ProjectWorkflowNavigationInput): boolean {
  return (
    !staticWebsiteOnly &&
    canReadWorkflow &&
    readiness?.status === "ready" &&
    readiness.workflow_enabled === true &&
    readiness.schema_ready === true
  );
}

export function projectWorkflowEntryEnabled(
  staticWebsiteOnly: boolean,
  canReadWorkflow: boolean,
  readiness: ProjectWorkflowControlPlaneReadiness | undefined,
): boolean {
  return projectWorkflowNavigationVisible({
    staticWebsiteOnly,
    canReadWorkflow,
    readiness,
  });
}
