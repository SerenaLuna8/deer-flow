import { notFound } from "next/navigation";

import { ProjectAuditPage } from "@/components/projects/governance/project-audit-page";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export default function ProjectAuditRoute() {
  if (isStaticWebsiteOnly()) notFound();
  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-8 sm:px-6 lg:px-8">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight">Project audit</h1>
        <p className="text-muted-foreground mt-3">
          Review privacy-safe project governance history.
        </p>
      </div>
      <ProjectAuditPage />
    </main>
  );
}
