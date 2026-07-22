import { ProjectChatWorkspace } from "@/components/projects/private-work/project-chat-workspace";

export default function ProjectChatsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <ProjectChatWorkspace>{children}</ProjectChatWorkspace>;
}
