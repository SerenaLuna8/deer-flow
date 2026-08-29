"use client";

import { PlusIcon } from "lucide-react";
import { useState } from "react";

import { useCurrentProject } from "@/components/projects/project-context";
import { ProjectPageHeader } from "@/components/projects/project-page-header";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import { useKnowledgeBases } from "@/core/knowledge/hooks";
import type { KnowledgeBaseItem } from "@/core/knowledge/types";
import { useProjectPrivateWorkScope } from "@/core/private-work/provider";

import { KnowledgeBaseDetail } from "./knowledge-base-detail";
import { KnowledgeBasesView } from "./knowledge-bases-view";
import { KnowledgeCreateWizard } from "./knowledge-create-wizard";

export function ProjectKnowledgePage() {
  const project = useCurrentProject();
  const { scope } = useProjectPrivateWorkScope();
  const { t } = useI18n();
  const labels = t.knowledge;
  const canEdit = project.capabilities.includes("shared_assets.edit");
  const [wizardOpen, setWizardOpen] = useState(false);
  const [selectedBase, setSelectedBase] = useState<KnowledgeBaseItem | null>(
    null,
  );
  const [createOpen, setCreateOpen] = useState(false);
  const bases = useKnowledgeBases(scope);
  // Settings edits and document uploads refresh the list; the open base must
  // reflect them (name, status, document count) without leaving the detail.
  const currentBase = selectedBase
    ? (bases.data?.items.find((item) => item.id === selectedBase.id) ??
      selectedBase)
    : null;

  if (canEdit && wizardOpen) {
    return (
      <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
        <KnowledgeCreateWizard
          scope={scope}
          onExit={() => setWizardOpen(false)}
          onCreateEmpty={() => {
            setWizardOpen(false);
            setCreateOpen(true);
          }}
          onFinished={(base) => {
            setWizardOpen(false);
            setSelectedBase(base);
          }}
        />
      </main>
    );
  }

  if (currentBase) {
    return (
      <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
        <KnowledgeBaseDetail
          scope={scope}
          base={currentBase}
          canEdit={canEdit}
          onBack={() => setSelectedBase(null)}
        />
      </main>
    );
  }

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
      <ProjectPageHeader
        className="mb-5"
        title={labels.page.title}
        description={labels.page.description}
        actions={
          canEdit ? (
            <Button type="button" onClick={() => setWizardOpen(true)}>
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
        onOpenBase={setSelectedBase}
      />
    </main>
  );
}
