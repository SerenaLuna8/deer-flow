"use client";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function projectThreadDeleteLandingPath(
  projectSlug: string,
  activeThreadId: string | undefined,
  deletedThreadId: string,
) {
  if (activeThreadId !== deletedThreadId) {
    return null;
  }
  return `/projects/${encodeURIComponent(projectSlug)}/chats`;
}

export function ProjectThreadDeleteDialog({
  open,
  title,
  pending,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  title: string;
  pending: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen && pending) {
          return;
        }
        onOpenChange(nextOpen);
      }}
    >
      <DialogContent
        showCloseButton={!pending}
        onEscapeKeyDown={(event) => {
          if (pending) event.preventDefault();
        }}
        onInteractOutside={(event) => {
          if (pending) event.preventDefault();
        }}
      >
        <DialogHeader>
          <DialogTitle>删除对话？</DialogTitle>
          <DialogDescription>
            “{title}”及其关联的侧边对话将被删除。此操作无法撤销。
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={() => onOpenChange(false)}
          >
            取消
          </Button>
          <Button
            type="button"
            variant="destructive"
            data-testid="project-thread-delete-confirm"
            disabled={pending}
            onClick={onConfirm}
          >
            {pending ? "正在删除…" : "确认删除"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
