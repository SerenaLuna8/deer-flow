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
import { Input } from "@/components/ui/input";

export function ProjectThreadRenameDialog({
  open,
  value,
  pending,
  onValueChange,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  value: string;
  pending: boolean;
  onValueChange: (value: string) => void;
  onOpenChange: (open: boolean) => void;
  onConfirm: (title: string) => void;
}) {
  const normalizedTitle = value.trim();

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen && pending) return;
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
        <form
          className="space-y-5"
          onSubmit={(event) => {
            event.preventDefault();
            if (normalizedTitle) onConfirm(normalizedTitle);
          }}
        >
          <DialogHeader>
            <DialogTitle>重命名会话</DialogTitle>
            <DialogDescription>
              输入一个便于在会话列表中识别的标题。
            </DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            value={value}
            maxLength={256}
            aria-label="会话标题"
            disabled={pending}
            onChange={(event) => onValueChange(event.target.value)}
            onFocus={(event) => event.currentTarget.select()}
          />
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
              type="submit"
              data-testid="project-thread-rename-confirm"
              disabled={pending || !normalizedTitle}
            >
              {pending ? "正在保存…" : "保存"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
