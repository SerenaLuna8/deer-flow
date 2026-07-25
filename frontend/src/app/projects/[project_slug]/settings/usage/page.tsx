import { notFound } from "next/navigation";

import { ProjectUsagePage } from "@/components/projects/governance/project-usage-page";
import { getI18n } from "@/core/i18n/server";
import { requireServerProjectCapability } from "@/core/projects/server-capability";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export default async function ProjectUsageRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  if (isStaticWebsiteOnly()) notFound();
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "project.usage.read");
  const { t } = await getI18n();
  const labels = t.project.governance.usage;
  return (
    <section className="min-w-0 space-y-6">
      <header className="max-w-2xl">
        <h2 className="text-2xl font-semibold tracking-tight">
          {labels.title}
        </h2>
        <p className="text-muted-foreground mt-2 leading-6">
          {labels.description}
        </p>
      </header>
      <ProjectUsagePage />
    </section>
  );
}
