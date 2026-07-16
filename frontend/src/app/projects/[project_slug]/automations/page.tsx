"use client";

import { ProjectAutomationsPage } from "@/components/projects/automations/project-automations-page";
import { useCurrentProject } from "@/components/projects/project-context";

export default function ProjectAutomationsRoute() {
  return <ProjectAutomationsPage project={useCurrentProject()} />;
}
