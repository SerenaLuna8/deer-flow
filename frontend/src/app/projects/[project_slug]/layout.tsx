import { ProjectContextProvider } from "@/components/projects/project-context";

export default async function ProjectLayout({
  children,
  params,
}: Readonly<{
  children: React.ReactNode;
  params: Promise<{ project_slug: string }>;
}>) {
  const { project_slug: slug } = await params;
  return (
    <ProjectContextProvider slug={slug}>{children}</ProjectContextProvider>
  );
}
