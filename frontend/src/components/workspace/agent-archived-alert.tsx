"use client";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";

export function AgentArchivedAlert({
  onStartNewChat,
}: {
  onStartNewChat?: () => void;
}) {
  const { t } = useI18n();
  return (
    <Alert
      variant="destructive"
      className="border-destructive/30 bg-destructive/5 mb-3"
      data-testid="agent-archived-alert"
    >
      <AlertTitle>{t.conversation.agentArchivedTitle}</AlertTitle>
      <AlertDescription className="flex items-center justify-between gap-3">
        <span>{t.conversation.agentArchivedDescription}</span>
        {onStartNewChat && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={onStartNewChat}
          >
            {t.conversation.agentArchivedAction}
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}
