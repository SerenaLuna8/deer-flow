import type { ReactNode } from "react";

import { requireServerProjectCapability } from "@/core/projects/server-capability";

export default async function ProjectConnectionsLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "project.channels.manage");
  return children;
}
