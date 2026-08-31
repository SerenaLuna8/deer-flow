"use client";

import { PlusIcon } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useState } from "react";

import { useCurrentProject } from "@/components/projects/project-context";
import { ProjectPageHeader } from "@/components/projects/project-page-header";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { useKnowledgeBases } from "@/core/knowledge/hooks";
import {
  buildKnowledgeSearch,
  KNOWLEDGE_DEFAULT_SORT,
  parseKnowledgeNavigation,
  type KnowledgeNavigationState,
} from "@/core/knowledge/navigation";
import { useProjectPrivateWorkScope } from "@/core/private-work/provider";

import { KnowledgeBaseDetail } from "./knowledge-base-detail";
import { KnowledgeBasesView } from "./knowledge-bases-view";
import { KnowledgeCreateWizard } from "./knowledge-create-wizard";

const KNOWLEDGE_PAGE_CLASS_NAME =
  "w-full min-w-0 px-4 py-6 text-[13px] leading-5 [--muted:#f0f2f5] [--primary:#155dfc] [--primary-foreground:#fff] [--ring:#60a5fa] [--selection:#155dfc] [--selection-subtle:#eff6ff] sm:px-6 lg:px-8 dark:[--muted:#24272e] dark:[--selection:#60a5fa] dark:[--selection-subtle:#172554]";

/**
 * URL-driven knowledge workspace: `kb/view/doc/segment/status/sort/page`
 * live in the query string (validated by the navigation module), so reloads
 * and browser history restore safe locations. Resource moves (base/document
 * open or close, view switches) push history entries; filter and page
 * changes replace the current one. Free-text state never enters the URL.
 */
export function ProjectKnowledgePage() {
  const project = useCurrentProject();
  const { scope } = useProjectPrivateWorkScope();
  const { t } = useI18n();
  const labels = t.knowledge;
  const canEdit = project.capabilities.includes("shared_assets.edit");
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const navState = useMemo(
    () => parseKnowledgeNavigation(searchParams),
    [searchParams],
  );
  const [wizardOpen, setWizardOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const bases = useKnowledgeBases(scope);

  const navigate = useCallback(
    (next: KnowledgeNavigationState, mode: "push" | "replace") => {
      const url = `${pathname}${buildKnowledgeSearch(next)}`;
      if (mode === "push") router.push(url, { scroll: false });
      else router.replace(url, { scroll: false });
    },
    [pathname, router],
  );

  // Entering a base is a fresh location: another base's filters, paging, or
  // open document must not leak into it.
  const openBase = useCallback(
    (baseId: string | null) =>
      navigate(
        {
          kb: baseId,
          view: "documents",
          doc: null,
          segment: null,
          status: null,
          sort: KNOWLEDGE_DEFAULT_SORT,
          page: 1,
        },
        "push",
      ),
    [navigate],
  );

  // The URL names the base; the polled authoritative list resolves it. A
  // deleted or foreign id must show "inaccessible" — never a cached object.
  const currentBase =
    navState.kb === null
      ? null
      : (bases.data?.items.find((item) => item.id === navState.kb) ?? null);

  if (canEdit && wizardOpen) {
    return (
      <main className={KNOWLEDGE_PAGE_CLASS_NAME}>
        <KnowledgeCreateWizard
          scope={scope}
          onExit={() => setWizardOpen(false)}
          onCreateEmpty={() => {
            setWizardOpen(false);
            setCreateOpen(true);
          }}
          onFinished={(base) => {
            setWizardOpen(false);
            openBase(base.id);
          }}
        />
      </main>
    );
  }

  if (navState.kb !== null) {
    if (currentBase === null) {
      return (
        <main className={KNOWLEDGE_PAGE_CLASS_NAME}>
          {bases.data === undefined ? (
            <Skeleton className="h-40 rounded-xl" />
          ) : (
            <div className="rounded-xl border border-dashed px-4 py-12 text-center">
              <p className="text-muted-foreground text-[13px]">
                {labels.detail.baseNotFound}
              </p>
              <Button
                type="button"
                variant="outline"
                className="mt-4 h-9 rounded-lg text-[13px] shadow-none"
                onClick={() => openBase(null)}
              >
                {labels.detail.backToBases}
              </Button>
            </div>
          )}
        </main>
      );
    }

    // Readers may hold a metadata/settings URL without the edit capability;
    // the URL is not an authorization, so the view degrades to documents.
    const view =
      !canEdit && (navState.view === "metadata" || navState.view === "settings")
        ? "documents"
        : navState.view;

    return (
      <main className={KNOWLEDGE_PAGE_CLASS_NAME}>
        <KnowledgeBaseDetail
          key={currentBase.id}
          scope={scope}
          base={currentBase}
          canEdit={canEdit}
          navState={{ ...navState, view }}
          onNavigate={navigate}
        />
      </main>
    );
  }

  return (
    <main className={KNOWLEDGE_PAGE_CLASS_NAME}>
      <ProjectPageHeader
        className="mb-5 [&_h1]:text-xl [&_p]:text-[13px]"
        title={labels.page.title}
        description={labels.page.description}
        actions={
          canEdit ? (
            <Button
              type="button"
              className="h-9 rounded-lg text-[13px] shadow-none"
              onClick={() => setWizardOpen(true)}
            >
              <PlusIcon aria-hidden className="size-4" />
              {labels.bases.createButton}
            </Button>
          ) : null
        }
      />
      <KnowledgeBasesView
        scope={scope}
        canEdit={canEdit}
        createOpen={createOpen}
        onCreateOpenChange={setCreateOpen}
        onStartWizard={() => setWizardOpen(true)}
        onOpenBase={(base) => openBase(base.id)}
      />
    </main>
  );
}
