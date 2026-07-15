"use client";

import { ProjectConnectionsPage } from "@/components/projects/private-work/project-connections-page";
import { useCurrentProject } from "@/components/projects/project-context";

export default function ProjectConnectionsRoute() {
  return <ProjectConnectionsPage project={useCurrentProject()} />;
}
