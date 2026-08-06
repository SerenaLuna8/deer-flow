"use client";

import { useCurrentProject } from "@/components/projects/project-context";

import { ProjectConversationRail } from "./project-conversation-rail";

export function ProjectChatWorkspace({
  children,
}: {
  children: React.ReactNode;
}) {
  const project = useCurrentProject();
  return (
    <div
      className="relative flex h-[calc(100vh-3.5rem)] min-h-0 min-w-0 overflow-hidden md:h-screen"
      data-testid="project-chat-workspace"
    >
      <ProjectConversationRail project={project} />
      <section className="min-h-0 min-w-0 flex-1 overflow-hidden">
        {children}
      </section>
    </div>
  );
}
