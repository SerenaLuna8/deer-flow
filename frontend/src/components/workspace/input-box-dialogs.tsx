import { Loader2Icon, Undo2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type InputBoxDialogLabels = {
  followupConfirmTitle: string;
  followupConfirmDescription: string;
  followupConfirmAppend: string;
  followupConfirmReplace: string;
  dreamRestoreConfirmTitle: string;
  dreamRestoreConfirmDescription: string;
  dreamRestoreConfirmAction: string;
};

export function FollowupConfirmDialog({
  open,
  cancelLabel,
  labels,
  onOpenChange,
  onCancel,
  onAppend,
  onReplace,
}: {
  open: boolean;
  cancelLabel: string;
  labels: InputBoxDialogLabels;
  onOpenChange: (open: boolean) => void;
  onCancel: () => void;
  onAppend: () => void;
  onReplace: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{labels.followupConfirmTitle}</DialogTitle>
          <DialogDescription>
            {labels.followupConfirmDescription}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button variant="secondary" onClick={onAppend}>
            {labels.followupConfirmAppend}
          </Button>
          <Button onClick={onReplace}>{labels.followupConfirmReplace}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function DreamRestoreConfirmDialog({
  version,
  restoring,
  cancelLabel,
  labels,
  onOpenChange,
  onCancel,
  onConfirm,
}: {
  version: number | null;
  restoring: boolean;
  cancelLabel: string;
  labels: InputBoxDialogLabels;
  onOpenChange: (open: boolean) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={version !== null} onOpenChange={onOpenChange}>
      <DialogContent closeLabel={cancelLabel}>
        <DialogHeader>
          <DialogTitle>
            {labels.dreamRestoreConfirmTitle.replace(
              "{version}",
              String(version ?? ""),
            )}
          </DialogTitle>
          <DialogDescription>
            {labels.dreamRestoreConfirmDescription}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={restoring}
            onClick={onCancel}
          >
            {cancelLabel}
          </Button>
          <Button type="button" disabled={restoring} onClick={onConfirm}>
            {restoring ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              <Undo2Icon className="size-4" />
            )}
            {labels.dreamRestoreConfirmAction}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
