import { WorkspaceLiveLayout } from "@/app/workspace/workspace-live-layout";
import { ProjectWorkbenchPage } from "@/components/projects/project-workbench-page";

export function WorkspacePage() {
  return <ProjectWorkbenchPage />;
}

export async function WorkspaceLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return <WorkspaceLiveLayout>{children}</WorkspaceLiveLayout>;
}
