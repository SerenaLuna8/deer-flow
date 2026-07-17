import { notFound } from "next/navigation";

import { ProjectUsagePage } from "@/components/projects/governance/project-usage-page";
import { getI18n } from "@/core/i18n/server";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export default async function ProjectUsageRoute() {
  if (isStaticWebsiteOnly()) notFound();
  const { t } = await getI18n();
  const labels = t.project.governance.usage;
  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-8 sm:px-6 lg:px-8">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight">
          {labels.title}
        </h1>
        <p className="text-muted-foreground mt-3">{labels.description}</p>
      </div>
      <ProjectUsagePage />
    </main>
  );
}
