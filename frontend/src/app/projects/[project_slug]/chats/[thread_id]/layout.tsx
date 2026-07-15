import { ProjectChatProviders } from "@/components/projects/private-work/project-chat-providers";

export default function ProjectChatLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <ProjectChatProviders>{children}</ProjectChatProviders>;
}
