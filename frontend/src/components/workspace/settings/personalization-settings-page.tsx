"use client";

import { BrainIcon, LoaderCircleIcon, RotateCcwIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import {
  useAccountPersonalization,
  useResetAccountMemory,
  useUpdateAccountPersonalization,
} from "@/core/account-personalization";
import { GatewayApiError } from "@/core/api/errors";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";

import { SettingsSection } from "./settings-section";

function mutationMessage(
  error: unknown,
  conflict: string,
  fallback: string,
): string {
  if (error instanceof GatewayApiError && error.status === 409) return conflict;
  return fallback;
}

export function PersonalizationSettingsPage() {
  const { user } = useAuth();
  const { t } = useI18n();
  const copy = t.settings.personalization;
  const accountId = user?.id ?? null;
  const preference = useAccountPersonalization(accountId);
  const updateMemory = useUpdateAccountPersonalization(accountId);
  const resetMemory = useResetAccountMemory(accountId);
  const [resetOpen, setResetOpen] = useState(false);

  const handleToggle = (memoryEnabled: boolean) => {
    if (!preference.data) return;
    updateMemory.mutate(
      {
        memoryEnabled,
        expectedVersion: preference.data.version,
      },
      {
        onSuccess: () =>
          toast.success(
            memoryEnabled ? copy.enableSuccess : copy.disableSuccess,
          ),
        onError: (error) =>
          toast.error(mutationMessage(error, copy.conflict, copy.updateError)),
      },
    );
  };

  const handleReset = () => {
    if (!preference.data) return;
    resetMemory.mutate(
      {
        confirm: true,
        expectedVersion: preference.data.version,
      },
      {
        onSuccess: () => {
          setResetOpen(false);
          toast.success(copy.resetSuccess);
        },
        onError: (error) =>
          toast.error(mutationMessage(error, copy.conflict, copy.resetError)),
      },
    );
  };

  return (
    <SettingsSection title={copy.title} description={copy.description}>
      {preference.isPending ? (
        <div className="text-muted-foreground flex items-center gap-2 rounded-lg border px-4 py-8 text-sm">
          <LoaderCircleIcon className="size-4 animate-spin" />
          {copy.loading}
        </div>
      ) : preference.isError || !preference.data ? (
        <div className="rounded-lg border px-4 py-5">
          <p className="text-sm font-medium">{copy.loadError}</p>
          <p className="text-muted-foreground mt-1 text-sm">
            {copy.loadErrorDescription}
          </p>
          <Button
            className="mt-4"
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void preference.refetch()}
          >
            {copy.retry}
          </Button>
        </div>
      ) : (
        <div className="divide-y rounded-xl border">
          <div className="flex flex-col gap-4 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0 pr-2">
              <div className="flex items-center gap-2 text-sm font-medium">
                <BrainIcon className="text-muted-foreground size-4" />
                {copy.enableTitle}
              </div>
              <p className="text-muted-foreground mt-1 text-sm">
                {copy.enableDescription}
              </p>
              {!preference.data.platformMemoryAvailable && (
                <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
                  {copy.platformUnavailable}
                </p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-2 self-end sm:self-auto">
              {updateMemory.isPending && (
                <span className="text-muted-foreground text-xs">
                  {copy.saving}
                </span>
              )}
              <Switch
                checked={preference.data.memoryEnabled}
                disabled={updateMemory.isPending || resetMemory.isPending}
                onCheckedChange={handleToggle}
                aria-label={copy.enableTitle}
              />
            </div>
          </div>

          <div className="flex flex-col gap-4 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0 pr-2">
              <div className="flex items-center gap-2 text-sm font-medium">
                <RotateCcwIcon className="text-muted-foreground size-4" />
                {copy.resetTitle}
              </div>
              <p className="text-muted-foreground mt-1 text-sm">
                {copy.resetDescription}
              </p>
            </div>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              className="self-end sm:self-auto"
              disabled={updateMemory.isPending || resetMemory.isPending}
              onClick={() => setResetOpen(true)}
            >
              {copy.resetButton}
            </Button>
          </div>
        </div>
      )}

      <Dialog open={resetOpen} onOpenChange={setResetOpen}>
        <DialogContent closeLabel={t.common.close}>
          <DialogHeader>
            <DialogTitle>{copy.resetDialogTitle}</DialogTitle>
            <DialogDescription>{copy.resetDialogDescription}</DialogDescription>
          </DialogHeader>
          <div className="bg-muted rounded-md px-3 py-2 text-sm font-medium">
            {copy.resetChatNotice}
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={resetMemory.isPending}
              onClick={() => setResetOpen(false)}
            >
              {copy.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={resetMemory.isPending || !preference.data}
              onClick={handleReset}
            >
              {resetMemory.isPending ? copy.resetting : copy.confirmReset}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </SettingsSection>
  );
}
