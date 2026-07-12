"use client";

import { useCurrentProject } from "./project-context";
import { ProjectHome } from "./project-home";

export function ProjectHomeLoader() {
  return <ProjectHome project={useCurrentProject()} />;
}
