import { notFound } from "next/navigation";

import { ProjectAuditPage } from "@/components/projects/governance/project-audit-page";
import { getI18n } from "@/core/i18n/server";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export default async function ProjectAuditRoute() {
  if (isStaticWebsiteOnly()) notFound();
  const { t } = await getI18n();
  const labels = t.project.governance.audit;
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
      <ProjectAuditPage />
    </section>
  );
}
