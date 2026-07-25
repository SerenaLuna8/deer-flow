"use client";

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

const SKILL_DELETE_DELAY_MS = 5_000;

export function skillDeleteSecondsRemaining(
  startedAt: number,
  now: number,
): number {
  return Math.min(
    5,
    Math.max(0, Math.ceil((startedAt + SKILL_DELETE_DELAY_MS - now) / 1_000)),
  );
}

export function ProjectSkillDeleteConfirmation({
  skillName,
  remainingSeconds,
  pending,
  errorMessage,
  onCancel,
  onConfirm,
}: {
  skillName: string;
  remainingSeconds: number;
  pending: boolean;
  errorMessage: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const waiting = remainingSeconds > 0;

  return (
    <>
      <DialogHeader>
        <DialogTitle>永久删除 Skill？</DialogTitle>
        <DialogDescription>
          将永久删除整个 Skill 包“{skillName}”，包括包内所有版本与文件。
          此操作不可恢复。
        </DialogDescription>
      </DialogHeader>
      {errorMessage ? (
        <p role="alert" className="text-destructive text-sm">
          {errorMessage}
        </p>
      ) : null}
      <DialogFooter>
        <Button
          type="button"
          variant="outline"
          disabled={pending}
          onClick={onCancel}
        >
          取消
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={pending || waiting}
          onClick={onConfirm}
        >
          {pending
            ? "删除中…"
            : waiting
              ? `确认删除（${remainingSeconds} 秒）`
              : "确认永久删除"}
        </Button>
      </DialogFooter>
    </>
  );
}

export function ProjectSkillDeleteDialog({
  skillName,
  startedAt,
  pending,
  errorMessage,
  onOpenChange,
  onConfirm,
}: {
  skillName: string;
  startedAt: number;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  const remainingSeconds = skillDeleteSecondsRemaining(startedAt, now);

  useEffect(() => {
    const interval = window.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= startedAt + SKILL_DELETE_DELAY_MS) {
        window.clearInterval(interval);
      }
    }, 250);
    return () => window.clearInterval(interval);
  }, [startedAt]);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !pending) onOpenChange(false);
      }}
    >
      <DialogContent
        showCloseButton={!pending}
        onEscapeKeyDown={(event) => pending && event.preventDefault()}
        onInteractOutside={(event) => pending && event.preventDefault()}
      >
        <ProjectSkillDeleteConfirmation
          skillName={skillName}
          remainingSeconds={remainingSeconds}
          pending={pending}
          errorMessage={errorMessage}
          onCancel={() => onOpenChange(false)}
          onConfirm={() => {
            if (!pending && remainingSeconds === 0) onConfirm();
          }}
        />
      </DialogContent>
    </Dialog>
  );
}
