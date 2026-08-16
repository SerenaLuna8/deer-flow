"use client";

import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import {
  MODEL_OUTPUT_LIMIT,
  OUTPUT_DELIVERY_INCOMPLETE,
  type ProjectRunFailureCode,
} from "@/core/private-work/api-client";

export function canRetryModelOutputLimit({
  canRun,
  isRunLoading,
  hasRegenerationTarget,
  retrySurfaceAvailable,
}: {
  canRun: boolean;
  isRunLoading: boolean;
  hasRegenerationTarget: boolean;
  retrySurfaceAvailable: boolean;
}): boolean {
  return (
    canRun && !isRunLoading && hasRegenerationTarget && retrySurfaceAvailable
  );
}

export function RunFailureAlert({
  failureCode,
  retryDisabled = false,
  onRetryWithoutThinking,
}: {
  failureCode: ProjectRunFailureCode | null;
  retryDisabled?: boolean;
  onRetryWithoutThinking?: () => boolean | Promise<boolean>;
}) {
  const { t } = useI18n();
  const [retrying, setRetrying] = useState(false);
  const isModelOutputLimit = failureCode === MODEL_OUTPUT_LIMIT;
  const isOutputDeliveryIncomplete = failureCode === OUTPUT_DELIVERY_INCOMPLETE;

  return (
    <Alert
      variant="destructive"
      className="border-destructive/30 bg-destructive/5 mb-3"
      data-testid="run-failure-alert"
      data-run-failure-code={failureCode ?? "generic"}
    >
      <AlertTitle>
        {isModelOutputLimit
          ? t.conversation.modelOutputLimitTitle
          : isOutputDeliveryIncomplete
            ? t.conversation.outputDeliveryIncompleteTitle
            : t.conversation.runFailedTitle}
      </AlertTitle>
      <AlertDescription className="flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
        <span>
          {isModelOutputLimit
            ? t.conversation.modelOutputLimitDescription
            : isOutputDeliveryIncomplete
              ? t.conversation.outputDeliveryIncompleteDescription
              : t.conversation.runFailedDescription}
        </span>
        {isModelOutputLimit && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="shrink-0"
            disabled={retrying || retryDisabled || !onRetryWithoutThinking}
            onClick={() => {
              if (!onRetryWithoutThinking) {
                return;
              }
              setRetrying(true);
              void Promise.resolve()
                .then(onRetryWithoutThinking)
                .catch(() => false)
                .finally(() => {
                  setRetrying(false);
                });
            }}
          >
            {retrying
              ? t.conversation.modelOutputLimitRetrying
              : t.conversation.modelOutputLimitRetry}
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}
