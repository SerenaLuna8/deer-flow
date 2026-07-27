import { BotIcon } from "lucide-react";

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

  const agentAssetId = thread.metadata?.agent_asset_id;
  const agentScope = thread.metadata?.agent_scope;
  if (
    typeof agentAssetId !== "string" ||
    (agentScope !== "project" && agentScope !== "system")
  ) {
    return { displayName: null, available: false };
  }
  if (!catalogSettled) return null;

  const items =
    agentScope === "project" ? catalog?.project_items : catalog?.system_items;
  const agent = items?.find((item) => item.id === agentAssetId);
  const displayName = agent?.display_name.trim();
  if (!displayName) {
    return { displayName: null, available: false };
  }
  return { displayName, available: true };
}

export function ThreadAgentIndicator({
  identity,
  className,
}: {
  identity: ThreadAgentIdentity | null;
  className?: string;
}) {
  if (!identity) return null;

  const label =
    identity.available && identity.displayName ? identity.displayName : "不可用";
  return (
    <span
      className={cn(
        "border-border/70 bg-background/80 text-muted-foreground flex min-w-0 shrink items-center gap-1 rounded-full border px-2 py-1 text-xs font-normal",
        className,
      )}
      aria-label={`当前 Agent：${label}`}
      data-testid="thread-agent-indicator"
      title={`当前 Agent：${label}`}
    >
      <BotIcon aria-hidden className="size-3.5 shrink-0" />
      <span className="shrink-0">Agent</span>
      <span aria-hidden>·</span>
      <span className="truncate text-foreground">{label}</span>
    </span>
  );
}
