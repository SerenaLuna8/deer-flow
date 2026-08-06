"use client";

import { useCurrentProject } from "./project-context";
import { ProjectHome } from "./project-home";
import { ProjectTokenUsageSection } from "./project-token-usage-section";

export function ProjectHomeLoader() {
  const project = useCurrentProject();
  const tokenUsageSection = project.capabilities.includes(
    "project.usage.read",
  ) ? (
    <ProjectTokenUsageSection />
  ) : null;

  return (
    <ProjectHome project={project} tokenUsageSection={tokenUsageSection} />
  );
}
