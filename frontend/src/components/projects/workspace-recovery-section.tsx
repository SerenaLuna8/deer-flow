"use client";

import {
  ArchiveRestoreIcon,
  ChevronDownIcon,
  RotateCcwIcon,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import {
  useRecoverableProjects,
  useRestoreProject,
} from "@/core/projects/hooks";
import type { Project } from "@/core/projects/types";

import { projectErrorMessage } from "./project-view-model";

export function formatRecoveryDeadline(
  value: string | null | undefined,
  locale: string,
  fallback: string,
): string {
  if (!value) return fallback;
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return fallback;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(timestamp);
}

function RecoverableProjectRow({
  project,
  userId,
}: {
  project: Project;
  userId: string;
}) {
  const { locale, t } = useI18n();
  const copy = t.projectWorkspace.recovery;
  const restore = useRestoreProject(userId, project.id);
  const [confirmOpen, setConfirmOpen] = useState(false);
  return (
    <>
      <li className="bg-muted/40 flex flex-col gap-3 rounded-xl p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="font-medium">{project.display_name}</p>
          <p className="text-muted-foreground mt-1 text-xs">
            {copy.recoverableUntil(
              formatRecoveryDeadline(
                project.deletion_effective_at,
                locale,
                copy.windowEnd,
              ),
            )}
          </p>
          {restore.error && (
            <p role="alert" className="text-destructive mt-2 text-sm">
              {projectErrorMessage(restore.error, t.projectWorkspace.errors)}
            </p>
          )}
        </div>
        <Button
          type="button"
          variant="outline"
          disabled={restore.isPending}
          onClick={() => setConfirmOpen(true)}
        >
          <RotateCcwIcon aria-hidden className="size-4" />
          {copy.recover}
        </Button>
      </li>
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{copy.confirmTitle}</DialogTitle>
            <DialogDescription>
              {copy.confirmDescription(project.display_name)}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setConfirmOpen(false)}
            >
              {copy.cancel}
            </Button>
            <Button
              type="button"
              disabled={restore.isPending}
              onClick={() =>
                restore.mutate(undefined, {
                  onSuccess: () => setConfirmOpen(false),
                })
              }
            >
              {restore.isPending ? copy.restoring : copy.confirm}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function WorkspaceRecoverySection({ userId }: { userId: string }) {
  const { t } = useI18n();
  const copy = t.projectWorkspace.recovery;
  const projects = useRecoverableProjects(userId);
  const recoverable =
    projects.data?.items.filter(
      (project) => project.status === "pending_deletion",
    ) ?? [];

  return (
    <section
      aria-labelledby="workspace-recovery-title"
      className="border-border/80 bg-card mb-8 overflow-hidden rounded-xl border"
    >
      <Collapsible defaultOpen>
        <h2 id="workspace-recovery-title">
          <CollapsibleTrigger
            data-testid="workspace-recovery-toggle"
            className="group hover:bg-muted/50 focus-visible:ring-ring/50 flex w-full items-center justify-between gap-4 px-5 py-4 text-left transition-colors outline-none focus-visible:ring-[3px] focus-visible:ring-inset"
          >
            <span className="flex items-center gap-2 font-semibold">
              <ArchiveRestoreIcon aria-hidden className="text-primary size-5" />
              {copy.title}
            </span>
            <ChevronDownIcon
              aria-hidden
              className="text-muted-foreground size-4 transition-transform group-data-[state=open]:rotate-180"
            />
          </CollapsibleTrigger>
        </h2>
        <CollapsibleContent>
          <div className="border-t px-5 py-5">
            {projects.isLoading ? (
              <Skeleton className="h-16 rounded-xl" />
            ) : projects.error ? (
              <p role="alert" className="text-muted-foreground text-sm">
                {projectErrorMessage(projects.error, t.projectWorkspace.errors)}
              </p>
            ) : recoverable.length ? (
              <ul className="space-y-3">
                {recoverable.map((project) => (
                  <RecoverableProjectRow
                    key={project.id}
                    project={project}
                    userId={userId}
                  />
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground text-sm">{copy.empty}</p>
            )}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}
