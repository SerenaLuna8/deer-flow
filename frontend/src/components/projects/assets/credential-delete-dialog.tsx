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

const CREDENTIAL_DELETE_DELAY_MS = 5_000;

export type CredentialDeleteSnapshot = Readonly<{
  credentialId: string;
  credentialName: string;
  expectedCredentialVersion: number;
  startedAt: number;
}>;

export function createCredentialDeleteSnapshot(
  credential: Pick<
    {
      id: string;
      display_name: string;
      version: number;
    },
    "id" | "display_name" | "version"
  >,
  startedAt: number,
): CredentialDeleteSnapshot {
  return Object.freeze({
    credentialId: credential.id,
    credentialName: credential.display_name,
    expectedCredentialVersion: credential.version,
    startedAt,
  });
}

export function credentialDeleteSecondsRemaining(
  startedAt: number,
  now: number,
): number {
  return Math.min(
    5,
    Math.max(
      0,
      Math.ceil((startedAt + CREDENTIAL_DELETE_DELAY_MS - now) / 1_000),
    ),
  );
}

export function CredentialDeleteConfirmation({
  credentialName,
  remainingSeconds,
  pending,
  errorMessage,
  onCancel,
  onConfirm,
}: {
  credentialName: string;
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
        <DialogTitle>删除 Credential？</DialogTitle>
        <DialogDescription>
          删除“{credentialName}”后，其所有版本会从普通列表与运行时查询中移除，
          相关 MCP Grant 与 Skill
          环境变量绑定将失效。仅审计记录保留，此操作不可恢复。
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
              : "确认删除"}
        </Button>
      </DialogFooter>
    </>
  );
}

export function CredentialDeleteDialog({
  snapshot,
  pending,
  errorMessage,
  onOpenChange,
  onConfirm,
}: {
  snapshot: CredentialDeleteSnapshot;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  const remainingSeconds = credentialDeleteSecondsRemaining(
    snapshot.startedAt,
    now,
  );

  useEffect(() => {
    const interval = window.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= snapshot.startedAt + CREDENTIAL_DELETE_DELAY_MS) {
        window.clearInterval(interval);
      }
    }, 250);
    return () => window.clearInterval(interval);
  }, [snapshot.startedAt]);

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
        <CredentialDeleteConfirmation
          credentialName={snapshot.credentialName}
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
