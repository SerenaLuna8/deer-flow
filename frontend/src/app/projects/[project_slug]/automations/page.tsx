import { notFound } from "next/navigation";

import { ProjectAutomationsRouteClient } from "@/components/projects/automations/project-automations-page";
import { PROJECT_AUTOMATION } from "@/core/projects/features";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export default function ProjectAutomationsRoute() {
  if (!PROJECT_AUTOMATION || isStaticWebsiteOnly()) notFound();
  return <ProjectAutomationsRouteClient />;
}
