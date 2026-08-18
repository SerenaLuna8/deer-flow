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

export const PROJECT_THREAD_TITLE_MAX_LENGTH = 200;

export function projectThreadRenameTitleState(value: string): {
  canSubmit: boolean;
  characterCount: number;
  normalizedTitle: string;
  tooLong: boolean;
} {
  const characterCount = Array.from(value).length;
  const normalizedTitle = value.trim();
  const tooLong = characterCount > PROJECT_THREAD_TITLE_MAX_LENGTH;
  return {
    canSubmit: Boolean(normalizedTitle) && !tooLong,
    characterCount,
    normalizedTitle,
    tooLong,
  };
}

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
  const { canSubmit, characterCount, normalizedTitle, tooLong } =
    projectThreadRenameTitleState(value);

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
            if (canSubmit) onConfirm(normalizedTitle);
          }}
        >
          <DialogHeader>
            <DialogTitle>重命名会话</DialogTitle>
            <DialogDescription>
              {`输入一个便于在会话列表中识别的标题，最多 ${PROJECT_THREAD_TITLE_MAX_LENGTH} 个字符。`}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Input
              autoFocus
              value={value}
              aria-label="会话标题"
              aria-describedby="project-thread-title-length"
              aria-invalid={tooLong || undefined}
              disabled={pending}
              onChange={(event) => onValueChange(event.target.value)}
              onFocus={(event) => event.currentTarget.select()}
            />
            <p
              id="project-thread-title-length"
              role={tooLong ? "alert" : undefined}
              className={
                tooLong
                  ? "text-destructive text-sm"
                  : "text-muted-foreground text-xs"
              }
            >
              {tooLong
                ? `会话标题不能超过 ${PROJECT_THREAD_TITLE_MAX_LENGTH} 个字符，当前 ${characterCount} 个字符。`
                : `${characterCount} / ${PROJECT_THREAD_TITLE_MAX_LENGTH}`}
            </p>
          </div>
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
              disabled={pending || !canSubmit}
            >
              {pending ? "正在保存…" : "保存"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
