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
import { useI18n } from "@/core/i18n/hooks";

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
  const { t } = useI18n();
  return (
    <>
      <DialogHeader>
        <DialogTitle>{t.adminAssets.dialogs.deleteTitle}</DialogTitle>
        <DialogDescription>
          {t.adminAssets.dialogs.deleteDescription(credentialName)}
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
          {t.adminAssets.dialogs.cancel}
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={pending || waiting}
          onClick={onConfirm}
        >
          {pending
            ? t.adminAssets.dialogs.deleting
            : waiting
              ? t.adminAssets.dialogs.confirmDeleteCountdown(remainingSeconds)
              : t.adminAssets.dialogs.confirmDelete}
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
