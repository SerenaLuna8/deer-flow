"use client";

import { ProjectUsageDimensionsSection } from "./governance/project-usage-page";
import { useCurrentProject } from "./project-context";
import { ProjectHome } from "./project-home";
import { ProjectTokenUsageSection } from "./project-token-usage-section";

export function ProjectHomeLoader() {
  const project = useCurrentProject();
  const canReadUsage = project.capabilities.includes("project.usage.read");
  const tokenUsageSection = canReadUsage ? <ProjectTokenUsageSection /> : null;
  const usageDimensionsSection = canReadUsage ? (
    <ProjectUsageDimensionsSection />
  ) : null;

  return (
    <ProjectHome
      project={project}
      tokenUsageSection={tokenUsageSection}
      usageDimensionsSection={usageDimensionsSection}
    />
  );
}
