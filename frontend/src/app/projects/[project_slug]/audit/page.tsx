import { notFound } from "next/navigation";

import { ProjectAuditPage } from "@/components/projects/governance/project-audit-page";
import { getI18n } from "@/core/i18n/server";
import { requireServerProjectCapability } from "@/core/projects/server-capability";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export default async function ProjectAuditRoute({
  params,
}: {
  params: Promise<{ project_slug: string }>;
}) {
  if (isStaticWebsiteOnly()) notFound();
  const { project_slug: slug } = await params;
  await requireServerProjectCapability(slug, "project.audit.read");
  const { t } = await getI18n();
  const labels = t.project.governance.audit;
  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <section className="min-w-0 space-y-3">
        <h1 className="text-2xl font-semibold tracking-tight">
          {labels.title}
        </h1>
        <ProjectAuditPage />
      </section>
    </main>
  );
}
