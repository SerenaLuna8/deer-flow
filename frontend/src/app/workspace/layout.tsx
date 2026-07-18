import { isStaticWebsiteOnly } from "@/core/static-mode";

export const dynamic = "force-dynamic";

export default async function WorkspaceLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  if (isStaticWebsiteOnly()) return children;
  const { WorkspaceLiveLayout } = await import("./workspace-live-layout");
  return <WorkspaceLiveLayout>{children}</WorkspaceLiveLayout>;
}
