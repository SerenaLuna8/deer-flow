"use client";

import { ProjectChatsPage } from "@/components/projects/private-work/project-chats-page";
import { useCurrentProject } from "@/components/projects/project-context";

export default function ProjectChatsRoute() {
  const project = useCurrentProject();
  return <ProjectChatsPage project={project} />;
}
