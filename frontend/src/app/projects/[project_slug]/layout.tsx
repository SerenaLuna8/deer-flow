import { notFound } from "next/navigation";

import { isStaticWebsiteOnly } from "@/core/static-mode";

export default async function ProjectLayout({
  children,
  params,
}: Readonly<{
  children: React.ReactNode;
  params: Promise<{ project_slug: string }>;
}>) {
  if (isStaticWebsiteOnly()) notFound();
  const { project_slug: slug } = await params;
  const { ProjectContextProvider } = await import(
    "@/components/projects/project-context"
  );
  return (
    <ProjectContextProvider slug={slug}>{children}</ProjectContextProvider>
  );
}
