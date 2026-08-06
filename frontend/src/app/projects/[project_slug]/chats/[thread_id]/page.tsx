"use client";

import { ProjectChatPage } from "@/components/projects/private-work/project-chat-page";
import { useCurrentProject } from "@/components/projects/project-context";

export default function ProjectChatRoute() {
  const project = useCurrentProject();
  return <ProjectChatPage project={project} />;
}
