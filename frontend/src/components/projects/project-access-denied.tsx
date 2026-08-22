"use client";

import { ShieldXIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";

export function ProjectAccessDenied({
  projectSlug,
  area,
}: {
  projectSlug: string;
  area: string;
}) {
  const { t } = useI18n();
  const labels = t.project.accessDenied;
  return (
    <main
      role="alert"
      data-error-status="403"
      className="mx-auto flex min-h-[50vh] max-w-xl flex-col items-center justify-center px-6 text-center"
    >
      <span className="bg-muted flex size-12 items-center justify-center rounded-2xl">
        <ShieldXIcon aria-hidden className="text-muted-foreground size-6" />
      </span>
      <h1 className="mt-5 text-2xl font-semibold">{labels.title}</h1>
      <p className="text-muted-foreground mt-3 text-sm leading-6">
        {labels.description(area)}
      </p>
      <Button asChild className="mt-6">
        <Link href={`/projects/${encodeURIComponent(projectSlug)}`}>
          {labels.backToProject}
        </Link>
      </Button>
    </main>
  );
}
