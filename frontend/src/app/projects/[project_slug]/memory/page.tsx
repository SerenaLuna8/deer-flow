"use client";

import { ProjectMemoryPage } from "@/components/projects/private-work/project-memory-page";
import { useCurrentProject } from "@/components/projects/project-context";

export default function ProjectMemoryRoute() {
  return <ProjectMemoryPage project={useCurrentProject()} />;
}
