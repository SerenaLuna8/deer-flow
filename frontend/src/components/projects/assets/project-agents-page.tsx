"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  BotIcon,
  LayoutGridIcon,
  ListIcon,
  Loader2Icon,
  MessageSquareIcon,
  PowerIcon,
  StarIcon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { AgentBuilderResumeBanner } from "@/components/projects/agents/agent-builder-resume-banner";
import {
  createProjectChatForAgent,
  type ExecutableProjectAgent,
} from "@/components/projects/private-work/agent-selector-dialog";
import { ProjectAgentStartContinuation } from "@/components/projects/private-work/project-agent-start-continuation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
} from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  agentBuilderCanAuthor,
  agentBuilderCanRead,
  useAgentBuilderSessionByAgent,
  useAgentBuilderSessions,
} from "@/core/agent-builder";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import type { Capability, Project } from "@/core/projects/types";
import {
  useChangeProjectAssetStatus,
  useProjectDefaultAgent,
  useSetProjectDefaultAgent,
  type AssetVersion,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { invalidateStoppedThreadCaches } from "@/core/threads/hooks";
import { cn } from "@/lib/utils";

import { useCurrentProject } from "../project-context";

import {
  cacheProjectAgentAuthoringReload,
  projectAgentAuthoringCacheEpochs,
  reloadProjectAgentAuthoringState,
  type AgentAssetVersion as AuthoringAgentVersion,
} from "./agent-authoring-recovery";
import { AgentCapabilityWorkbench } from "./agent-capability-workbench";
import { AgentInstructionsWorkbench } from "./agent-instructions-workbench";
import { ProjectAssetPageShell } from "./project-asset-page-shell";
import {
  isMainProjectAgent,
  useAgentMcpDependencyRuntime,
} from "./use-mcp-dependency-runtime";

export type ProjectAgentViewMode = "cards" | "list";

type AgentAssetVersion = Extract<AssetVersion, { agent_id: string }>;

function AgentDetailWorkbench({
  accountId,
  projectId,
  item,
  version,
  canAuthor,
  editing,
  onEditingChange,
  onDirtyChange,
  onVersionCreated,
}: {
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: AgentAssetVersion | null;
  canAuthor: boolean;
  editing: boolean;
  onEditingChange: (editing: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
  onVersionCreated: (versionId: string) => void;
}) {
  const { t } = useI18n();
  const project = useCurrentProject();
  const copy = t.agents.catalog;
  const queryClient = useQueryClient();
  const privateWork = usePrivateWorkAccess();
  const designSessionQuery = useAgentBuilderSessionByAgent(
    accountId,
    projectId,
    item.id,
    item.scope === "project",
  );
  const [instructionDirty, setInstructionDirty] = useState(false);
  const [capabilityDirty, setCapabilityDirty] = useState(false);
  const [authoringSnapshot, setAuthoringSnapshot] = useState<{
    item: ProjectAssetItem;
    version: AuthoringAgentVersion;
  } | null>(null);
  const [authoringPreparationPending, setAuthoringPreparationPending] =
    useState(false);
  const [authoringPreparationError, setAuthoringPreparationError] = useState<
    string | null
  >(null);
  const preparationAbortRef = useRef<AbortController | null>(null);
  const preparationGenerationRef = useRef(0);
  const mountedRef = useRef(false);
  const scopeKey = `${accountId}:${projectId}:${item.id}`;
  const scopeKeyRef = useRef(scopeKey);
  const handleInstructionDirty = useCallback(
    (dirty: boolean) => setInstructionDirty(dirty),
    [],
  );
  const handleCapabilityDirty = useCallback(
    (dirty: boolean) => setCapabilityDirty(dirty),
    [],
  );

  useEffect(() => {
    onDirtyChange(instructionDirty || capabilityDirty);
  }, [capabilityDirty, instructionDirty, onDirtyChange]);

  useEffect(() => {
    mountedRef.current = true;
    scopeKeyRef.current = scopeKey;
    return () => {
      mountedRef.current = false;
      preparationAbortRef.current?.abort();
      preparationAbortRef.current = null;
      preparationGenerationRef.current += 1;
    };
  }, [scopeKey]);

  useEffect(
    () => () => {
      onDirtyChange(false);
    },
    [onDirtyChange],
  );

  const prepareAuthoring = useCallback(
    async (includeDependencyCatalogs = false): Promise<boolean> => {
      preparationAbortRef.current?.abort();
      const controller = new AbortController();
      const generation = preparationGenerationRef.current + 1;
      preparationAbortRef.current = controller;
      preparationGenerationRef.current = generation;
      setAuthoringPreparationPending(true);
      setAuthoringPreparationError(null);
      const startedAt = projectAgentAuthoringCacheEpochs({
        queryClient,
        accountId,
        projectId,
        assetId: item.id,
      });
      try {
        const reload = await runPrivateWorkAbortable(
          privateWork,
          (scopeSignal) =>
            reloadProjectAgentAuthoringState({
              projectId,
              assetId: item.id,
              minimumRevision: item.revision,
              includeDependencyCatalogs,
              signal: scopeSignal
                ? AbortSignal.any([controller.signal, scopeSignal])
                : controller.signal,
            }),
        );
        if (
          !mountedRef.current ||
          scopeKeyRef.current !== scopeKey ||
          !isPrivateWorkAccessActive(privateWork) ||
          controller.signal.aborted ||
          preparationAbortRef.current !== controller ||
          preparationGenerationRef.current !== generation
        ) {
          return false;
        }
        await cacheProjectAgentAuthoringReload({
          queryClient,
          accountId,
          projectId,
          assetId: item.id,
          reload,
          startedAt,
          isCurrent: () =>
            mountedRef.current &&
            scopeKeyRef.current === scopeKey &&
            isPrivateWorkAccessActive(privateWork) &&
            !controller.signal.aborted &&
            preparationAbortRef.current === controller &&
            preparationGenerationRef.current === generation,
        });
        if (
          !mountedRef.current ||
          scopeKeyRef.current !== scopeKey ||
          !isPrivateWorkAccessActive(privateWork) ||
          controller.signal.aborted ||
          preparationAbortRef.current !== controller ||
          preparationGenerationRef.current !== generation
        ) {
          return false;
        }
        setAuthoringSnapshot({
          item: reload.item,
          version: reload.version,
        });
        return true;
      } catch {
        if (
          mountedRef.current &&
          scopeKeyRef.current === scopeKey &&
          isPrivateWorkAccessActive(privateWork) &&
          !controller.signal.aborted &&
          preparationAbortRef.current === controller &&
          preparationGenerationRef.current === generation
        ) {
          setAuthoringPreparationError(copy.authoringLoadFailed);
        }
        return false;
      } finally {
        if (preparationAbortRef.current === controller) {
          preparationAbortRef.current = null;
          if (mountedRef.current && scopeKeyRef.current === scopeKey) {
            setAuthoringPreparationPending(false);
          }
        }
      }
    },
    [
      accountId,
      item.id,
      item.revision,
      privateWork,
      projectId,
      queryClient,
      scopeKey,
      copy.authoringLoadFailed,
    ],
  );

  const handleInstructionEditingChange = useCallback(
    (nextEditing: boolean) => {
      if (!nextEditing) {
        onEditingChange(false);
        return;
      }
      void prepareAuthoring(false).then((ready) => {
        if (ready) onEditingChange(true);
      });
    },
    [onEditingChange, prepareAuthoring],
  );

  const authoringVersion = authoringSnapshot?.version ?? version;
  const authoringItem = authoringSnapshot?.item ?? item;
  const preparationStatus = authoringPreparationPending ? (
    <p role="status" className="text-muted-foreground text-sm">
      {copy.authoringLoading}
    </p>
  ) : authoringPreparationError ? (
    <p role="alert" className="text-destructive text-sm">
      {authoringPreparationError}
    </p>
  ) : null;

  if (item.scope !== "project") {
    return (
      <div className="space-y-3">
        {preparationStatus}
        <AgentInstructionsWorkbench
          accountId={accountId}
          projectId={projectId}
          item={authoringItem}
          version={authoringVersion}
          canAuthor={canAuthor}
          authoringPreparationPending={authoringPreparationPending}
          editing={editing}
          onEditingChange={handleInstructionEditingChange}
          onDirtyChange={handleInstructionDirty}
          onVersionCreated={onVersionCreated}
        />
      </div>
    );
  }

  return (
    <Tabs defaultValue="instructions" className="gap-5">
      {preparationStatus}
      {designSessionQuery.data ? (
        <div>
          <Button asChild size="sm" variant="outline">
            <Link
              href={`/projects/${encodeURIComponent(project.slug)}/agents/new/${encodeURIComponent(designSessionQuery.data.id)}`}
            >
              {copy.viewDesignRecord}
            </Link>
          </Button>
        </div>
      ) : null}
      <TabsList aria-label={copy.detailTabsAria}>
        <TabsTrigger value="instructions">{copy.instructionsTab}</TabsTrigger>
        <TabsTrigger value="capabilities">{copy.capabilitiesTab}</TabsTrigger>
      </TabsList>
      <TabsContent
        value="instructions"
        forceMount
        className="data-[state=inactive]:hidden"
      >
        <AgentInstructionsWorkbench
          accountId={accountId}
          projectId={projectId}
          item={authoringItem}
          version={authoringVersion}
          canAuthor={canAuthor}
          authoringPreparationPending={authoringPreparationPending}
          editing={editing}
          onEditingChange={handleInstructionEditingChange}
          onDirtyChange={handleInstructionDirty}
          onVersionCreated={onVersionCreated}
        />
      </TabsContent>
      <TabsContent
        value="capabilities"
        forceMount
        className="data-[state=inactive]:hidden"
      >
        <AgentCapabilityWorkbench
          accountId={accountId}
          projectId={projectId}
          item={authoringItem}
          version={authoringVersion}
          canAuthor={canAuthor}
          authoringPreparationPending={authoringPreparationPending}
          onBeginEditing={() => prepareAuthoring(true)}
          onDirtyChange={handleCapabilityDirty}
          onVersionCreated={onVersionCreated}
        />
      </TabsContent>
    </Tabs>
  );
}

export function sortProjectAgentsWithDefaultFirst(
  items: readonly ProjectAssetItem[],
  defaultAgentId: string | null | undefined,
): ProjectAssetItem[] {
  if (!defaultAgentId) return [...items];
  return items
    .map((item, index) => ({ item, index }))
    .sort((left, right) => {
      const leftDefault = left.item.id === defaultAgentId;
      const rightDefault = right.item.id === defaultAgentId;
      if (leftDefault !== rightDefault) return leftDefault ? -1 : 1;
      return left.index - right.index;
    })
    .map(({ item }) => item);
}

export function ProjectAgentViewToggle({
  value,
  onChange,
}: {
  value: ProjectAgentViewMode;
  onChange: (value: ProjectAgentViewMode) => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.catalog;
  return (
    <div
      role="group"
      aria-label={copy.viewModeAria}
      className="border-border bg-background inline-flex h-10 overflow-hidden rounded-lg border p-0.5"
    >
      {(
        [
          ["cards", copy.cards, LayoutGridIcon],
          ["list", copy.list, ListIcon],
        ] as const
      ).map(([mode, label, Icon]) => {
        const selected = value === mode;
        return (
          <button
            key={mode}
            type="button"
            aria-pressed={selected}
            className={cn(
              "focus-visible:ring-ring inline-flex min-w-20 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:outline-none",
              selected
                ? "bg-foreground text-background"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
            onClick={() => onChange(mode)}
          >
            <Icon aria-hidden className="size-4" />
            {label}
          </button>
        );
      })}
    </div>
  );
}

export type ProjectAgentChatAvailability = {
  enabled: boolean;
  reason: string | null;
};

export function projectAgentChatAvailability(
  item: ProjectAssetItem,
  projectCapabilities: readonly Capability[],
  copy: Translations["agents"]["catalog"],
  mcpDependencyReason: string | null = null,
): ProjectAgentChatAvailability {
  if (
    !projectCapabilities.includes("private_work.create") ||
    !projectCapabilities.includes("shared_assets.execute")
  ) {
    return { enabled: false, reason: copy.chatForbidden };
  }
  if (item.status !== "active") {
    return { enabled: false, reason: copy.unavailable };
  }
  if (!item.capabilities.includes("shared_assets.execute")) {
    return { enabled: false, reason: copy.executeForbidden };
  }
  if (!item.current_version_id) {
    return { enabled: false, reason: copy.currentVersionRequired };
  }
  if (mcpDependencyReason) {
    return { enabled: false, reason: mcpDependencyReason };
  }
  return { enabled: true, reason: null };
}

export function projectAgentCanActivate(
  item: ProjectAssetItem,
  projectCapabilities: readonly Capability[],
): boolean {
  return (
    item.status === "suspended" &&
    projectCapabilities.includes("shared_assets.manage_bindings") &&
    item.capabilities.includes("shared_assets.manage_bindings") &&
    item.current_version_id !== null
  );
}

export function projectAgentDefaultAvailability(
  item: ProjectAssetItem,
  projectCapabilities: readonly Capability[],
  copy: Translations["agents"]["catalog"],
  mcpDependencyReason: string | null = null,
): ProjectAgentChatAvailability {
  if (
    !projectCapabilities.includes("shared_assets.manage_bindings") ||
    !item.capabilities.includes("shared_assets.manage_bindings")
  ) {
    return { enabled: false, reason: copy.defaultAdminOnly };
  }
  if (item.scope !== "project" || item.status !== "active") {
    return { enabled: false, reason: copy.defaultUnavailable };
  }
  if (!item.capabilities.includes("shared_assets.execute")) {
    return { enabled: false, reason: copy.executeForbidden };
  }
  if (!item.current_version_id) {
    return { enabled: false, reason: copy.currentVersionRequired };
  }
  if (mcpDependencyReason) {
    return { enabled: false, reason: mcpDependencyReason };
  }
  return { enabled: true, reason: null };
}

function projectMainDefaultAvailability(
  item: ProjectAssetItem,
  projectCapabilities: readonly Capability[],
  copy: Translations["agents"]["catalog"],
): ProjectAgentChatAvailability {
  if (!isMainProjectAgent(item)) {
    return { enabled: false, reason: copy.systemDefaultUnavailable };
  }
  if (!projectCapabilities.includes("shared_assets.manage_bindings")) {
    return { enabled: false, reason: copy.defaultAdminOnly };
  }
  if (item.status !== "active") {
    return { enabled: false, reason: copy.mainUnavailable };
  }
  if (!item.capabilities.includes("shared_assets.execute")) {
    return { enabled: false, reason: copy.mainExecuteForbidden };
  }
  if (!item.current_version_id) {
    return { enabled: false, reason: copy.mainVersionUnavailable };
  }
  return { enabled: true, reason: null };
}

function ProjectAgentCollectionView({
  items,
  source,
  viewMode,
  projectCapabilities,
  selectedAssetId,
  creatingChatForAgentId,
  activatingAgentId = null,
  defaultAgentId,
  settingDefaultAgentTarget = null,
  defaultAgentLoading = false,
  defaultAgentError = false,
  mcpDependencyReasons = new Map(),
  onSelect,
  onStartChat,
  onActivate,
  onSetDefault,
  onSetMainDefault,
}: {
  items: ProjectAssetItem[];
  source: "system" | "project";
  viewMode: ProjectAgentViewMode;
  projectCapabilities: readonly Capability[];
  selectedAssetId: string | null;
  creatingChatForAgentId: string | null;
  activatingAgentId?: string | null;
  defaultAgentId?: string | null;
  settingDefaultAgentTarget?: string | "main" | null;
  defaultAgentLoading?: boolean;
  defaultAgentError?: boolean;
  mcpDependencyReasons?: ReadonlyMap<string, string>;
  onSelect: (item: ProjectAssetItem) => void;
  onStartChat: (item: ExecutableProjectAgent) => void;
  onActivate?: (item: ProjectAssetItem) => void;
  onSetDefault?: (item: ProjectAssetItem) => void;
  onSetMainDefault?: () => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.catalog;
  if (items.length === 0) {
    return (
      <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-10 text-center text-sm">
        {source === "system" ? copy.emptySystem : copy.emptyProject}
      </p>
    );
  }

  return (
    <div
      role="list"
      data-agent-view={viewMode}
      className={cn(
        viewMode === "cards"
          ? "grid gap-3 sm:grid-cols-2 lg:gap-4 xl:grid-cols-3"
          : "overflow-hidden rounded-xl border",
      )}
    >
      {items.map((item) => {
        const availability = projectAgentChatAvailability(
          item,
          projectCapabilities,
          copy,
          mcpDependencyReasons.get(item.id) ?? null,
        );
        const creating = creatingChatForAgentId === item.id;
        const activating = activatingAgentId === item.id;
        const canActivate =
          projectAgentCanActivate(item, projectCapabilities) &&
          Boolean(onActivate);
        const main = isMainProjectAgent(item);
        const isDefault =
          defaultAgentId !== undefined &&
          (main ? defaultAgentId === null : defaultAgentId === item.id);
        const defaultAvailability = main
          ? projectMainDefaultAvailability(item, projectCapabilities, copy)
          : projectAgentDefaultAvailability(
              item,
              projectCapabilities,
              copy,
              mcpDependencyReasons.get(item.id) ?? null,
            );
        const canOfferMainDefault =
          main &&
          projectCapabilities.includes("shared_assets.manage_bindings") &&
          Boolean(onSetMainDefault);
        const canOfferProjectDefault =
          item.scope === "project" &&
          projectCapabilities.includes("shared_assets.manage_bindings") &&
          item.capabilities.includes("shared_assets.manage_bindings") &&
          Boolean(onSetDefault);
        const canSetDefault =
          !isDefault && (canOfferMainDefault || canOfferProjectDefault);
        const defaultPending = settingDefaultAgentTarget !== null;
        const settingThisDefault =
          settingDefaultAgentTarget === (main ? "main" : item.id);
        const defaultActionReason = defaultAgentError
          ? copy.defaultLoadFailed
          : defaultAgentLoading
            ? copy.defaultLoading
            : defaultAgentId === undefined
              ? copy.defaultUnknown
              : defaultAvailability.reason;
        const description = item.description?.trim();
        const defaultButton = canSetDefault ? (
          <Button
            type="button"
            variant="outline"
            size={viewMode === "list" ? "sm" : "default"}
            className={cn(viewMode === "cards" && "min-h-10 w-full")}
            disabled={
              defaultPending ||
              defaultAgentLoading ||
              defaultAgentError ||
              defaultAgentId === undefined ||
              !defaultAvailability.enabled
            }
            aria-label={
              defaultActionReason
                ? copy.setDefaultBlockedAria(
                    item.display_name,
                    defaultActionReason,
                  )
                : copy.setDefaultAria(item.display_name)
            }
            title={defaultActionReason ?? undefined}
            onClick={() => (main ? onSetMainDefault?.() : onSetDefault?.(item))}
          >
            {settingThisDefault ? (
              <Loader2Icon aria-hidden className="size-4 animate-spin" />
            ) : (
              <StarIcon aria-hidden className="size-4" />
            )}
            {settingThisDefault ? copy.settingDefault : copy.setDefault}
          </Button>
        ) : null;
        const activateButton = canActivate ? (
          <Button
            type="button"
            variant="outline"
            size={viewMode === "list" ? "sm" : "default"}
            className={cn(viewMode === "cards" && "min-h-10 w-full")}
            disabled={activating}
            aria-label={copy.activateAria(item.display_name)}
            onClick={() => onActivate?.(item)}
          >
            {activating ? (
              <Loader2Icon aria-hidden className="size-4 animate-spin" />
            ) : (
              <PowerIcon aria-hidden className="size-4" />
            )}
            {activating ? copy.activating : copy.activate}
          </Button>
        ) : null;
        const chatButton = (
          <Button
            type="button"
            size={viewMode === "list" ? "sm" : "default"}
            className={cn(viewMode === "cards" && "min-h-10 w-full")}
            disabled={!availability.enabled || creating}
            aria-label={
              availability.reason
                ? copy.chatBlockedAria(item.display_name, availability.reason)
                : copy.chatAria(item.display_name)
            }
            title={availability.reason ?? undefined}
            onClick={() => onStartChat(item)}
          >
            <MessageSquareIcon aria-hidden className="size-4" />
            {creating ? copy.creatingChat : copy.chat}
          </Button>
        );
        const statusBadges = (
          <>
            {main ? <Badge variant="secondary">{copy.builtIn}</Badge> : null}
            {isDefault ? (
              <Badge variant="secondary">
                <StarIcon aria-hidden /> {copy.currentDefault}
              </Badge>
            ) : null}
            {item.status !== "active" ? (
              <AssetStatusBadge
                status={item.status}
                label={item.status === "suspended" ? copy.suspended : undefined}
              />
            ) : null}
          </>
        );

        if (viewMode === "list") {
          return (
            <div
              key={item.id}
              role="listitem"
              className={cn(
                "group flex flex-col border-b last:border-b-0 sm:flex-row sm:items-stretch",
                selectedAssetId === item.id
                  ? "bg-selection-subtle/60"
                  : "hover:bg-muted/40",
              )}
            >
              <button
                type="button"
                aria-haspopup="dialog"
                aria-label={copy.viewDetails(item.display_name)}
                className="focus-visible:ring-ring flex min-w-0 flex-1 items-center gap-4 px-5 py-4 text-left focus-visible:ring-2 focus-visible:outline-none focus-visible:ring-inset"
                onClick={() => onSelect(item)}
              >
                <span className="bg-muted flex size-10 shrink-0 items-center justify-center rounded-xl">
                  <BotIcon aria-hidden className="size-5" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-semibold sm:text-base">
                      {item.display_name}
                    </span>
                    {statusBadges}
                  </span>
                  <span className="text-muted-foreground mt-1 block truncate text-sm">
                    {description && description.length > 0
                      ? description
                      : main
                        ? copy.mainDescription
                        : copy.noDescription}
                  </span>
                </span>
              </button>
              <div className="border-border/70 flex shrink-0 items-center gap-2 border-t px-5 py-3 sm:border-t-0 sm:border-l">
                {defaultButton}
                {activateButton}
                {chatButton}
              </div>
            </div>
          );
        }

        return (
          <Card
            key={item.id}
            role="listitem"
            className={cn(
              "group gap-0 overflow-hidden py-0 transition-[border-color,box-shadow,transform] duration-200 hover:-translate-y-0.5 hover:shadow-md",
              selectedAssetId === item.id
                ? "border-selection ring-selection/15 ring-2"
                : "hover:border-foreground/20",
            )}
          >
            <button
              type="button"
              aria-haspopup="dialog"
              aria-label={copy.viewDetails(item.display_name)}
              className="focus-visible:ring-ring flex min-h-0 flex-col text-left focus-visible:ring-2 focus-visible:outline-none focus-visible:ring-inset"
              onClick={() => onSelect(item)}
            >
              <CardHeader className="w-full gap-0 px-5 pt-5">
                <span className="flex min-w-0 items-center gap-2.5">
                  <span className="bg-muted flex size-9 shrink-0 items-center justify-center rounded-lg">
                    <BotIcon aria-hidden className="size-4.5" />
                  </span>
                  <span className="min-w-0 flex-1 truncate text-base font-semibold">
                    {item.display_name}
                  </span>
                  {statusBadges}
                </span>
              </CardHeader>
              <CardContent className="w-full px-5 pt-3 pb-3">
                <span className="text-muted-foreground line-clamp-2 h-10 overflow-hidden text-sm leading-5">
                  {description && description.length > 0
                    ? description
                    : main
                      ? copy.mainDescription
                      : copy.noDescription}
                </span>
              </CardContent>
            </button>
            <CardFooter
              className={cn(
                "grid grid-cols-1 gap-2 px-5 pb-4",
                (canSetDefault || canActivate) && "sm:grid-cols-2",
              )}
            >
              {defaultButton}
              {activateButton}
              {chatButton}
            </CardFooter>
          </Card>
        );
      })}
    </div>
  );
}

export function ProjectAgentCatalogView({
  systemItems,
  projectItems,
  projectCapabilities,
  viewMode,
  selectedAssetId,
  creatingChatForAgentId,
  activatingAgentId = null,
  defaultAgentId,
  settingDefaultAgentTarget = null,
  defaultAgentLoading = false,
  defaultAgentError = false,
  mcpDependencyReasons = new Map(),
  mcpDependencyError = false,
  onSelect,
  onStartChat,
  onActivate,
  onSetDefault,
  onSetMainDefault,
}: {
  systemItems: ProjectAssetItem[];
  projectItems: ProjectAssetItem[];
  projectCapabilities: readonly Capability[];
  viewMode: ProjectAgentViewMode;
  selectedAssetId: string | null;
  creatingChatForAgentId: string | null;
  activatingAgentId?: string | null;
  defaultAgentId?: string | null;
  settingDefaultAgentTarget?: string | "main" | null;
  defaultAgentLoading?: boolean;
  defaultAgentError?: boolean;
  mcpDependencyReasons?: ReadonlyMap<string, string>;
  mcpDependencyError?: boolean;
  onSelect: (item: ProjectAssetItem) => void;
  onStartChat: (item: ExecutableProjectAgent) => void;
  onActivate?: (item: ProjectAssetItem) => void;
  onSetDefault?: (item: ProjectAssetItem) => void;
  onSetMainDefault?: () => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.catalog;
  const orderedSystemItems = [...systemItems].sort((left, right) => {
    const leftMain = isMainProjectAgent(left);
    const rightMain = isMainProjectAgent(right);
    return leftMain === rightMain ? 0 : leftMain ? -1 : 1;
  });
  const orderedProjectItems = sortProjectAgentsWithDefaultFirst(
    projectItems,
    defaultAgentId,
  );
  const sharedProps = {
    viewMode,
    projectCapabilities,
    selectedAssetId,
    creatingChatForAgentId,
    activatingAgentId,
    defaultAgentId,
    settingDefaultAgentTarget,
    defaultAgentLoading,
    defaultAgentError,
    mcpDependencyReasons,
    onSelect,
    onStartChat,
    onActivate,
    onSetDefault,
    onSetMainDefault,
  };

  return (
    <div className="space-y-8">
      {mcpDependencyError ? (
        <p role="alert" className="text-destructive text-sm">
          {copy.mcpValidationFailed}
        </p>
      ) : null}
      {defaultAgentError ? (
        <p role="alert" className="text-destructive text-sm">
          {copy.defaultLoadFailed}
        </p>
      ) : null}

      <section
        aria-labelledby="system-agent-section-title"
        className="space-y-3"
      >
        <div>
          <div className="flex items-center gap-2.5">
            <h2
              id="system-agent-section-title"
              className="text-lg font-semibold"
            >
              {copy.systemSection}
            </h2>
            <Badge variant="secondary" className="tabular-nums">
              {orderedSystemItems.length}
            </Badge>
          </div>
          <p className="text-muted-foreground mt-1 text-sm">
            {copy.systemDescription}
          </p>
        </div>
        <ProjectAgentCollectionView
          {...sharedProps}
          source="system"
          items={orderedSystemItems}
        />
      </section>

      <section
        aria-labelledby="project-agent-section-title"
        className="space-y-3"
      >
        <div>
          <div className="flex items-center gap-2.5">
            <h2
              id="project-agent-section-title"
              className="text-lg font-semibold"
            >
              {copy.projectSection}
            </h2>
            <Badge variant="secondary" className="tabular-nums">
              {orderedProjectItems.length}
            </Badge>
          </div>
          <p className="text-muted-foreground mt-1 text-sm">
            {copy.projectDescription}
          </p>
        </div>
        <ProjectAgentCollectionView
          {...sharedProps}
          source="project"
          items={orderedProjectItems}
        />
      </section>
    </div>
  );
}

function ProjectAgentCatalog({
  project,
  data,
  viewMode,
  selectedAssetId,
  onSelect,
}: {
  project: Project;
  data: Parameters<typeof ProjectAgentStartContinuation>[0]["catalog"];
  viewMode: ProjectAgentViewMode;
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.catalog;
  const router = useRouter();
  const queryClient = useQueryClient();
  const privateWork = usePrivateWorkAccess();
  const [creatingChatForAgentId, setCreatingChatForAgentId] = useState<
    string | null
  >(null);
  const changeStatus = useChangeProjectAssetStatus(
    privateWork.scope.accountId,
    project.id,
    "agents",
  );
  const defaultAgent = useProjectDefaultAgent(
    privateWork.scope.accountId,
    project.id,
  );
  const setDefaultAgent = useSetProjectDefaultAgent(
    privateWork.scope.accountId,
    project.id,
  );
  const systemItems = data.system_items.filter(
    (item) => isMainProjectAgent(item) || item.binding?.enabled === true,
  );
  const projectItems = data.project_items;
  const items = [...systemItems, ...projectItems];
  const mcpDependencyRuntime = useAgentMcpDependencyRuntime({
    accountId: privateWork.scope.accountId,
    projectId: project.id,
    agents: items,
    enabled:
      (project.capabilities.includes("private_work.create") &&
        project.capabilities.includes("shared_assets.execute")) ||
      project.capabilities.includes("shared_assets.manage_bindings"),
  });
  const mcpDependencyReasons = new Map(
    mcpDependencyRuntime.assessments.flatMap((assessment, index) => {
      const item = items[index];
      return item && assessment.status !== "ready" && assessment.reason
        ? [[item.id, assessment.reason] as const]
        : [];
    }),
  );

  async function startChat(agent: ExecutableProjectAgent) {
    const availability = projectAgentChatAvailability(
      agent,
      project.capabilities,
      copy,
      mcpDependencyReasons.get(agent.id) ?? null,
    );
    if (!availability.enabled || creatingChatForAgentId) return;

    setCreatingChatForAgentId(agent.id);
    try {
      await createProjectChatForAgent({
        scope: privateWork.scope,
        projectSlug: project.slug,
        agent,
        threadDisplayName: t.agents.newChat.threadName,
        invalidateThreadLists: () =>
          invalidateStoppedThreadCaches(
            queryClient,
            null,
            false,
            privateWork.scope,
          ),
        navigate: (path) => router.push(path),
      });
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : copy.createChatFailed,
      );
    } finally {
      setCreatingChatForAgentId(null);
    }
  }

  function activate(agent: ProjectAssetItem) {
    if (
      !projectAgentCanActivate(agent, project.capabilities) ||
      changeStatus.isPending
    ) {
      return;
    }
    changeStatus.mutate(
      {
        assetId: agent.id,
        action: "enable",
        input: { expected_revision: agent.revision },
      },
      {
        onSuccess: () => toast.success(copy.activated(agent.display_name)),
        onError: (error) =>
          toast.error(adminAssetErrorMessage(error, t.adminAssets.errors)),
      },
    );
  }

  function updateDefaultAgent(agent: ProjectAssetItem) {
    const availability = projectAgentDefaultAvailability(
      agent,
      project.capabilities,
      copy,
      mcpDependencyReasons.get(agent.id) ?? null,
    );
    if (
      !availability.enabled ||
      !defaultAgent.data ||
      defaultAgent.error ||
      setDefaultAgent.isPending
    ) {
      return;
    }
    setDefaultAgent.mutate(
      {
        agent_asset_id: agent.id,
        expected_revision: defaultAgent.data.revision,
      },
      {
        onSuccess: () => toast.success(copy.defaultSet(agent.display_name)),
        onError: (error) =>
          toast.error(adminAssetErrorMessage(error, t.adminAssets.errors)),
      },
    );
  }

  function setMainDefault() {
    const mainAgent = systemItems.find(isMainProjectAgent);
    if (
      !mainAgent ||
      !projectMainDefaultAvailability(mainAgent, project.capabilities, copy)
        .enabled ||
      !defaultAgent.data ||
      defaultAgent.error ||
      setDefaultAgent.isPending
    ) {
      return;
    }
    setDefaultAgent.mutate(
      {
        agent_asset_id: null,
        expected_revision: defaultAgent.data.revision,
      },
      {
        onSuccess: () => toast.success(copy.mainDefaultSet),
        onError: (error) =>
          toast.error(adminAssetErrorMessage(error, t.adminAssets.errors)),
      },
    );
  }

  return (
    <ProjectAgentCatalogView
      systemItems={systemItems}
      projectItems={projectItems}
      projectCapabilities={project.capabilities}
      viewMode={viewMode}
      selectedAssetId={selectedAssetId}
      creatingChatForAgentId={creatingChatForAgentId}
      activatingAgentId={
        changeStatus.isPending
          ? (changeStatus.variables?.assetId ?? null)
          : null
      }
      defaultAgentId={defaultAgent.data?.agent_asset_id}
      settingDefaultAgentTarget={
        setDefaultAgent.isPending
          ? (setDefaultAgent.variables?.agent_asset_id ?? "main")
          : null
      }
      defaultAgentLoading={defaultAgent.isLoading}
      defaultAgentError={Boolean(defaultAgent.error)}
      mcpDependencyReasons={mcpDependencyReasons}
      mcpDependencyError={Boolean(mcpDependencyRuntime.error)}
      onSelect={onSelect}
      onStartChat={(agent) => void startChat(agent)}
      onActivate={activate}
      onSetDefault={updateDefaultAgent}
      onSetMainDefault={setMainDefault}
    />
  );
}

function ProjectAgentBuilderLead({
  project,
  data,
  startChatIntent,
  startChatIntentId,
}: {
  project: Project;
  data: Parameters<typeof ProjectAgentStartContinuation>[0]["catalog"];
  startChatIntent: boolean;
  startChatIntentId: string | null;
}) {
  const { user } = useAuth();
  const canRead = agentBuilderCanRead(project.capabilities);
  const canAuthor = agentBuilderCanAuthor(project.capabilities);
  const sessions = useAgentBuilderSessions(
    user?.id ?? "",
    project.id,
    Boolean(user && canRead),
  );

  return (
    <>
      {user && canRead ? (
        <AgentBuilderResumeBanner
          accountId={user.id}
          projectId={project.id}
          projectSlug={project.slug}
          sessions={sessions.data ?? []}
          canAuthor={canAuthor}
        />
      ) : null}
      <ProjectAgentStartContinuation
        project={project}
        catalog={data}
        requested={startChatIntent}
        intentId={startChatIntentId}
      />
    </>
  );
}

export function ProjectAgentsPage({
  startChatIntent = false,
  startChatIntentId = null,
  selectedAssetId = null,
}: {
  startChatIntent?: boolean;
  startChatIntentId?: string | null;
  selectedAssetId?: string | null;
}) {
  const { t } = useI18n();
  const [viewMode, setViewMode] = useState<ProjectAgentViewMode>("cards");

  return (
    <ProjectAssetPageShell
      kind="agents"
      title={t.agents.catalog.title}
      layout="agent-cards"
      headerActions={
        <ProjectAgentViewToggle value={viewMode} onChange={setViewMode} />
      }
      initialSelectedAssetId={selectedAssetId}
      selectionQueryParam="agent_id"
      renderLead={({ project, data }) => (
        <ProjectAgentBuilderLead
          project={project}
          data={data}
          startChatIntent={startChatIntent}
          startChatIntentId={startChatIntentId}
        />
      )}
      renderList={({ project, data, selectedAssetId, onSelect }) => (
        <ProjectAgentCatalog
          project={project}
          data={data}
          viewMode={viewMode}
          selectedAssetId={selectedAssetId}
          onSelect={onSelect}
        />
      )}
      renderAssetEditor={(version, context) => (
        <AgentDetailWorkbench
          key={version?.id ?? "empty"}
          accountId={context.accountId}
          projectId={context.projectId}
          item={context.item}
          version={version && "agent_id" in version ? version : null}
          canAuthor={context.canAuthor}
          editing={context.editing}
          onEditingChange={context.onEditingChange}
          onDirtyChange={context.onDirtyChange}
          onVersionCreated={context.onVersionCreated}
        />
      )}
    />
  );
}
