import { ProjectHomeLoader } from "@/components/projects/project-home-loader";

export default async function ProjectPage({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  const { project_slug: slug } = await params;
  return <ProjectHomeLoader slug={slug} />;
}
