"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import { useRequestProjectDeletion } from "@/core/projects/hooks";

import { useCurrentProject } from "../project-context";
import { projectErrorMessage } from "../project-view-model";

export function ProjectLifecyclePanel() {
  const project = useCurrentProject();
  const { user } = useAuth();
  const router = useRouter();
  const deletion = useRequestProjectDeletion(user?.id, project.id);
  const { t } = useI18n();
  const labels = t.project.settings.lifecycle;
  const [confirmOpen, setConfirmOpen] = useState(false);
  const canManage = project.capabilities.includes("project.lifecycle.manage");

  if (!canManage) return null;

  return (
    <section
      aria-labelledby="project-lifecycle-title"
      className="border-destructive/30 bg-destructive/5 rounded-2xl border p-6"
    >
      <h2 id="project-lifecycle-title" className="text-lg font-semibold">
        {labels.title}
      </h2>
      <p className="text-muted-foreground mt-2 max-w-2xl text-sm">
        {labels.description}
      </p>
      {deletion.error && (
        <p role="alert" className="text-destructive mt-3 text-sm">
          {projectErrorMessage(deletion.error, t.projectWorkspace.errors)}
        </p>
      )}
      <Button
        type="button"
        variant="destructive"
        className="mt-5"
        onClick={() => setConfirmOpen(true)}
      >
        {labels.requestDeletion}
      </Button>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{labels.confirmTitle}</DialogTitle>
            <DialogDescription>{labels.confirmDescription}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              autoFocus
              disabled={deletion.isPending}
              onClick={() => setConfirmOpen(false)}
            >
              {labels.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deletion.isPending}
              onClick={() =>
                deletion.mutate(undefined, {
                  onSuccess: () => router.replace("/workspace"),
                })
              }
            >
              {labels.confirm}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
