import { RefreshCwIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { Translations } from "@/core/i18n";
import type { MemoryVersionSummary } from "@/core/private-work/memory/types";

export type ProjectMemoryCopy = Translations["projectMemory"];

export function memoryTriggerLabel(
  copy: ProjectMemoryCopy,
  trigger: MemoryVersionSummary["trigger"],
) {
  if (trigger === "auto_dream") return copy.autoDream;
  if (trigger === "manual_dream") return copy.manualDream;
  if (trigger === "budget_rewrite") return copy.budgetRewrite;
  return copy.restoreTrigger;
}

export function formatMemoryDate(value: string, locale: string) {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function MemoryErrorState({
  title,
  retryLabel,
  onRetry,
}: {
  title: string;
  retryLabel: string;
  onRetry: () => void;
}) {
  return (
    <Alert variant="destructive">
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-3"
          onClick={onRetry}
        >
          <RefreshCwIcon className="size-4" />
          {retryLabel}
        </Button>
      </AlertDescription>
    </Alert>
  );
}
