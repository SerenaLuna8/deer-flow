import { BotIcon, MessageSquarePlusIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

type ThreadAgentCatalogItem = {
  id: string;
  display_name: string;
};

export type ThreadAgentCatalog = {
  project_items: readonly ThreadAgentCatalogItem[];
  system_items: readonly ThreadAgentCatalogItem[];
};

export type ThreadAgentIdentity = {
  displayName: string | null;
  available: boolean;
};

export type ThreadAgentSelection = {
  agentAssetId: string;
  agentScope: "project" | "system";
};

export function resolveThreadAgentSelection(
  thread:
    | {
        metadata?: Record<string, unknown> | null;
      }
    | null
    | undefined,
): ThreadAgentSelection | null {
  const agentAssetId = thread?.metadata?.agent_asset_id;
  const agentScope = thread?.metadata?.agent_scope;
  if (
    typeof agentAssetId !== "string" ||
    (agentScope !== "project" && agentScope !== "system")
  ) {
    return null;
  }
  return { agentAssetId, agentScope };
}

export function resolveThreadAgentIdentity(
  thread:
    | {
        metadata?: Record<string, unknown> | null;
      }
    | null
    | undefined,
  catalog: ThreadAgentCatalog | undefined,
  catalogSettled: boolean,
): ThreadAgentIdentity | null {
  if (!thread) return null;

  const selection = resolveThreadAgentSelection(thread);
  if (!selection) {
    return { displayName: null, available: false };
  }
  if (!catalogSettled) return null;

  const items =
    selection.agentScope === "project"
      ? catalog?.project_items
      : catalog?.system_items;
  const agent = items?.find((item) => item.id === selection.agentAssetId);
  const displayName = agent?.display_name.trim();
  if (!displayName) {
    return { displayName: null, available: false };
  }
  return { displayName, available: true };
}

export function ThreadAgentIndicator({
  identity,
  onStartNewChat,
  className,
}: {
  identity: ThreadAgentIdentity | null;
  onStartNewChat?: () => void;
  className?: string;
}) {
  const { t } = useI18n();
  const copy = t.agents.indicator;
  if (!identity) return null;

  const label =
    identity.available && identity.displayName
      ? identity.displayName
      : copy.unavailable;
  const content = (
    <>
      <BotIcon aria-hidden className="size-3.5 shrink-0" />
      <span className="shrink-0">Agent</span>
      <span aria-hidden>·</span>
      <span className="text-foreground truncate">{label}</span>
      {onStartNewChat && (
        <MessageSquarePlusIcon aria-hidden className="ml-0.5 size-3.5" />
      )}
    </>
  );
  const classes = cn(
    "border-border/70 bg-background/80 text-muted-foreground flex min-w-0 shrink items-center gap-1 rounded-full border px-2 py-1 text-xs font-normal",
    className,
  );

  if (onStartNewChat) {
    const actionLabel = copy.startWithOther(label);
    return (
      <Button
        type="button"
        variant="outline"
        size="sm"
        className={cn(classes, "h-auto")}
        aria-label={actionLabel}
        data-testid="thread-agent-indicator"
        title={actionLabel}
        onClick={onStartNewChat}
      >
        {content}
      </Button>
    );
  }

  return (
    <span
      className={classes}
      aria-label={copy.current(label)}
      data-testid="thread-agent-indicator"
      title={copy.current(label)}
    >
      {content}
    </span>
  );
}
