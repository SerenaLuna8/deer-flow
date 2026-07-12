"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

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
import { useRequestProjectDeletion } from "@/core/projects/hooks";

import { useCurrentProject } from "../project-context";
import { projectErrorMessage } from "../project-view-model";

export function ProjectLifecyclePanel() {
  const project = useCurrentProject();
  const { user } = useAuth();
  const router = useRouter();
  const deletion = useRequestProjectDeletion(user?.id, project.id);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const canManage = project.capabilities.includes("project.lifecycle.manage");

  useEffect(() => {
    if (deletion.data) router.replace("/workspace");
  }, [deletion.data, router]);

  if (!canManage) return null;

  return (
    <section
      aria-labelledby="project-lifecycle-title"
      className="border-destructive/30 bg-destructive/5 mt-8 rounded-2xl border p-6"
    >
      <h2 id="project-lifecycle-title" className="text-lg font-semibold">
        项目生命周期
      </h2>
      <p className="text-muted-foreground mt-2 max-w-2xl text-sm">
        请求删除后，项目立即停止进入和治理，并在恢复窗口结束后永久删除。
      </p>
      {deletion.error && (
        <p role="alert" className="text-destructive mt-3 text-sm">
          {projectErrorMessage(deletion.error)}
        </p>
      )}
      <Button
        type="button"
        variant="destructive"
        className="mt-5"
        onClick={() => setConfirmOpen(true)}
      >
        请求删除项目
      </Button>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认删除项目</DialogTitle>
            <DialogDescription>
              项目会立即进入待删除状态，你将返回工作空间。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="destructive"
              disabled={deletion.isPending}
              onClick={() => deletion.mutate()}
            >
              确认请求删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
