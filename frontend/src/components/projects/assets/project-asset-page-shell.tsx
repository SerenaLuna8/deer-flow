"use client";

import {
  ArrowRightIcon,
  BotIcon,
  PlugZapIcon,
  PlusIcon,
  SearchIcon,
  SparklesIcon,
  UploadIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  CreateVersionDialog,
  type VersionAuthoringInput,
} from "@/components/admin/assets/admin-asset-dialogs";
import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { agentBuilderCanAuthor } from "@/core/agent-builder";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Project } from "@/core/projects/types";
import {
  useCreateProjectAssetVersion,
  useChangeProjectAssetStatus,
  useDisableProjectSystemBinding,
  useEnableProjectSystemBinding,
  useImportProjectSkillArchive,
  useProjectAssets,
  useSyncCurrentProjectSystemMcpBinding,
  assetIdSchema,
  type AssetKind,
  type AssetListKind,
  type AssetVersion,
  type DisableSystemBindingInput,
  type EnableCurrentSystemBindingInput,
  type EnableSystemBindingInput,
  type ProjectAssetItem,
  type ProjectAssetList,
  type ProjectMcpEditableConfigurationResponse,
  type SyncCurrentSystemMcpBindingInput,
} from "@/core/shared-assets";

import { useCurrentProject } from "../project-context";
import { ProjectPageHeader } from "../project-page-header";

import { ProjectAssetDetailSheet } from "./project-asset-detail-sheet";
import type { ProjectAssetVersionRenderContext } from "./project-asset-detail-sheet";
import {
  projectMcpStatusToggleState,
  projectSkillSecretSetupRequired,
  projectSkillStatusToggleState,
} from "./project-asset-view-model";
import { ProjectMcpCreateDialog } from "./project-mcp-create-dialog";
import { ProjectMcpEditDialog } from "./project-mcp-edit-dialog";
import {
  ProjectSkillImportDialog,
  projectSkillImportErrorMessage,
} from "./project-skill-import-dialog";
import { SystemBindingDialog } from "./system-binding-dialog";

type MutableAssetKind = AssetListKind;
export type ProjectAssetSourceFilter = "system" | "project";
export type ProjectAssetPageLayout = "default" | "agent-cards";
export type ProjectAssetListRenderContext = {
  project: Project;
  data: ProjectAssetList;
  items: ProjectAssetItem[];
  source: ProjectAssetSourceFilter;
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
};

const KIND_META = {
  agents: {
    singular: "Agent",
    icon: BotIcon,
  },
  skills: {
    singular: "Skill",
    icon: SparklesIcon,
  },
  "mcp-servers": {
    singular: "MCP",
    icon: PlugZapIcon,
  },
} as const;

const BINDING_KIND: Record<MutableAssetKind, AssetKind> = {
  agents: "agent",
  skills: "skill",
  "mcp-servers": "mcp",
};

export function SkillCreateMenuItems({
  projectSlug,
  onImport,
}: {
  projectSlug: string;
  onImport: () => void;
}) {
  return (
    <>
      <DropdownMenuItem asChild>
        <Link href={`/projects/${encodeURIComponent(projectSlug)}/skills/new`}>
          <SparklesIcon aria-hidden className="size-4" />
          AI 对话创建
        </Link>
      </DropdownMenuItem>
      <DropdownMenuItem onSelect={onImport}>
        <UploadIcon aria-hidden className="size-4" />
        上传压缩包
      </DropdownMenuItem>
    </>
  );
}

function assetAvailability(
  item: ProjectAssetItem,
  kind: AssetListKind,
): string {
  if (item.scope === "system") {
    if (!item.binding) return "未启用";
    if (!item.binding.enabled) return "已从项目停用";
    if (
      item.current_version_id &&
      item.current_version_id !== item.binding.current_version_id
    ) {
      return kind === "mcp-servers" ? "有新配置" : "有新版本";
    }
    return "已在项目启用";
  }
  if (!item.current_version_id) {
    return kind === "mcp-servers" ? "尚未生效" : "尚无当前版本";
  }
  return kind === "mcp-servers" ? "已有生效配置" : "已有当前版本";
}

export function filterProjectAssetItems(
  data: ProjectAssetList,
  searchQuery: string,
  source: ProjectAssetSourceFilter,
): ProjectAssetItem[] {
  const query = searchQuery.trim().toLocaleLowerCase();
  const items = source === "system" ? data.system_items : data.project_items;
  if (query === "") return items;
  return items.filter(
    (item) =>
      item.display_name.toLocaleLowerCase().includes(query) ||
      item.slug.toLocaleLowerCase().includes(query),
  );
}

export function projectAssetSelectionHref(
  pathname: string,
  currentSearch: string,
  queryParam: string,
  assetId: string | null,
  dependentQueryParams: readonly string[] = [],
): string {
  const params = new URLSearchParams(currentSearch);
  for (const dependentQueryParam of dependentQueryParams) {
    params.delete(dependentQueryParam);
  }
  if (assetId) {
    params.set(queryParam, assetId);
  } else {
    params.delete(queryParam);
  }
  const search = params.toString();
  return search ? `${pathname}?${search}` : pathname;
}

export function projectAssetSelectionFromSearch(
  currentSearch: string,
  queryParam: string,
): string | null {
  const values = new URLSearchParams(currentSearch).getAll(queryParam);
  if (values.length !== 1) return null;
  const parsed = assetIdSchema.safeParse(values[0]);
  return parsed.success ? parsed.data : null;
}

export function projectSkillExactVersionSelectionHref(
  pathname: string,
  currentSearch: string,
  skillId: string,
  skillVersionId: string,
  configureSecrets: boolean,
): string {
  const params = new URLSearchParams(currentSearch);
  params.set("skill_id", skillId);
  params.set("skill_version_id", skillVersionId);
  if (configureSecrets) {
    params.set("configure_secrets", "1");
  } else {
    params.delete("configure_secrets");
  }
  return `${pathname}?${params.toString()}`;
}

export function projectAssetSelectionDecision(
  currentAssetId: string | null,
  nextAssetId: string | null,
  dirty: boolean,
): "unchanged" | "apply" | "confirm-discard" {
  if (currentAssetId === nextAssetId) return "unchanged";
  return dirty ? "confirm-discard" : "apply";
}

export function defaultProjectAssetSource(
  data: Pick<ProjectAssetList, "system_items" | "project_items">,
): ProjectAssetSourceFilter {
  return data.project_items.length === 0 && data.system_items.length > 0
    ? "system"
    : "project";
}

export function projectAssetSourceOptions(
  kind: MutableAssetKind,
): ReadonlyArray<readonly [ProjectAssetSourceFilter, string]> {
  if (kind === "agents") return [["project", "项目自建"]];
  return [
    ["system", "系统提供"],
    ["project", "项目自建"],
  ];
}

export function projectAssetPrimaryActionLabel(kind: MutableAssetKind): string {
  return kind === "mcp-servers"
    ? "添加 MCP"
    : `新建 ${KIND_META[kind].singular}`;
}

export function projectAssetEmptyMessage(
  kind: MutableAssetKind,
  source: ProjectAssetSourceFilter,
): string {
  if (kind === "mcp-servers" && source === "project") {
    return "尚未添加项目 MCP。";
  }
  const sourceLabel = source === "system" ? "系统提供" : "项目自建";
  return `暂无${sourceLabel}的 ${KIND_META[kind].singular}。`;
}

export function projectAssetListErrorMessage(
  kind: MutableAssetKind,
  error: unknown,
): string {
  const detail = error
    ? adminAssetErrorMessage(error)
    : "服务未返回有效的资产列表。";
  return kind === "mcp-servers" ? `无法加载 MCP 列表。${detail}` : detail;
}

export function systemBindingToggleState(
  item: ProjectAssetItem,
  kind?: MutableAssetKind,
): {
  checked: boolean;
  disabled: boolean;
  targetVersionId: string | null;
} {
  const checked = item.scope === "system" && item.binding?.enabled === true;
  const canManage =
    item.scope === "system" &&
    item.capabilities.includes("shared_assets.manage_bindings") &&
    (item.status === "active" || checked);
  const targetVersionId =
    item.scope === "system"
      ? kind === "mcp-servers"
        ? item.current_version_id
        : (item.binding?.current_version_id ?? item.current_version_id)
      : null;
  return {
    checked,
    disabled: !canManage || (!checked && targetVersionId === null),
    targetVersionId,
  };
}

export function projectSystemSkillBindingCanManage(
  item: ProjectAssetItem,
  projectCanManageBindings: boolean,
): boolean {
  return (
    projectCanManageBindings &&
    item.scope === "system" &&
    item.capabilities.includes("shared_assets.manage_bindings") &&
    (item.status === "active" || item.binding?.enabled === true)
  );
}

export function systemMcpBindingNeedsUpdate(item: ProjectAssetItem): boolean {
  return (
    item.scope === "system" &&
    item.status === "active" &&
    item.capabilities.includes("shared_assets.manage_bindings") &&
    item.binding?.enabled === true &&
    item.current_version_id !== null &&
    item.binding.current_version_id !== item.current_version_id
  );
}

export type ProjectSystemBindingListAction =
  | {
      type: "enable";
      input: EnableSystemBindingInput | EnableCurrentSystemBindingInput;
    }
  | {
      type: "sync-current";
      assetId: string;
      input: SyncCurrentSystemMcpBindingInput;
    }
  | {
      type: "disable";
      assetId: string;
      input: DisableSystemBindingInput;
    };

export function projectSystemBindingListAction(
  kind: MutableAssetKind,
  item: ProjectAssetItem,
  checked: boolean,
  syncCurrent = false,
): ProjectSystemBindingListAction | null {
  const state = systemBindingToggleState(item, kind);
  if (syncCurrent) {
    if (kind !== "mcp-servers" || !systemMcpBindingNeedsUpdate(item)) {
      return null;
    }
    return {
      type: "sync-current",
      assetId: item.id,
      input: item.binding
        ? { expected_binding_version: item.binding.version }
        : {},
    };
  }
  if (state.disabled || state.checked === checked) return null;

  if (checked) {
    if (kind === "mcp-servers") {
      if (!item.current_version_id) return null;
      return {
        type: "sync-current",
        assetId: item.id,
        input: item.binding
          ? { expected_binding_version: item.binding.version }
          : {},
      };
    }
    if (!state.targetVersionId) return null;
    return {
      type: "enable",
      input: {
        asset_id: item.id,
        ...(item.binding
          ? { expected_binding_version: item.binding.version }
          : {}),
      },
    };
  }

  return item.binding?.enabled
    ? {
        type: "disable",
        assetId: item.id,
        input: { expected_binding_version: item.binding.version },
      }
    : null;
}

export type VersionDialogSubmissionToken = {
  assetId: string;
  generation: number;
};

export function rememberRequestedVersion(
  current: Record<string, string>,
  assetId: string,
  versionId: string,
): Record<string, string> {
  return current[assetId] === versionId
    ? current
    : { ...current, [assetId]: versionId };
}

export type ImportedSkillSelection = {
  assetId: string;
  versionId: string;
};

export function importedSkillSelectionReady(
  data: Pick<ProjectAssetList, "system_items" | "project_items"> | undefined,
  selection: ImportedSkillSelection | null,
): ImportedSkillSelection | null {
  if (
    !data ||
    !selection ||
    !data.project_items.some((item) => item.id === selection.assetId)
  ) {
    return null;
  }
  return selection;
}

export const configuredMcpSelectionReady = importedSkillSelectionReady;

export function projectSkillImportNeedsSecretSetup(
  version: Pick<
    Extract<AssetVersion, { skill_id: string }>,
    "secret_requirements"
  >,
): boolean {
  return version.secret_requirements.length > 0;
}

export function configuredMcpSuccessMessage(
  workflowStatus: "published" | "pending_approval",
): string {
  return workflowStatus === "published"
    ? "MCP 已添加并发布。"
    : "MCP 配置已保存，秘密尚未配置，因此暂未生效。";
}

export function createdProjectAssetSelectionReady(
  data: Pick<ProjectAssetList, "project_items"> | undefined,
  assetId: string | null,
): string | null {
  if (!data || !assetId) return null;
  return data.project_items.some((item) => item.id === assetId)
    ? assetId
    : null;
}

export const createdSkillSelectionReady = createdProjectAssetSelectionReady;

export function handleRequestedVersion(
  current: Record<string, string>,
  assetId: string,
  versionId: string,
): Record<string, string> {
  if (current[assetId] !== versionId) return current;
  const next = { ...current };
  delete next[assetId];
  return next;
}

export function versionDialogSubmissionMatches(
  active: VersionDialogSubmissionToken | null,
  submission: VersionDialogSubmissionToken,
): boolean {
  return (
    active?.assetId === submission.assetId &&
    active.generation === submission.generation
  );
}

export function closeCompletedVersionDialog(
  current: ProjectAssetItem | null,
  active: VersionDialogSubmissionToken | null,
  completed: VersionDialogSubmissionToken,
): ProjectAssetItem | null {
  return current?.id === completed.assetId &&
    versionDialogSubmissionMatches(active, completed)
    ? null
    : current;
}

function AssetList({
  kind,
  source,
  items,
  selectedAssetId,
  onSelect,
  onToggleSystemBinding,
  onSyncSystemMcpBinding,
  onToggleProjectAssetStatus,
  bindingIntent = null,
  bindingErrorAssetId,
  bindingError,
  projectStatusIntent = null,
  projectStatusErrorAssetId,
  projectStatusError,
}: {
  kind: MutableAssetKind;
  source: ProjectAssetSourceFilter;
  items: ProjectAssetItem[];
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
  onToggleSystemBinding: (item: ProjectAssetItem, checked: boolean) => void;
  onSyncSystemMcpBinding?: (item: ProjectAssetItem) => void;
  onToggleProjectAssetStatus?: (
    item: ProjectAssetItem,
    checked: boolean,
  ) => void;
  bindingIntent?: { assetId: string; checked: boolean } | null;
  bindingErrorAssetId?: string | null;
  bindingError?: unknown;
  projectStatusIntent?: { assetId: string; checked: boolean } | null;
  projectStatusErrorAssetId?: string | null;
  projectStatusError?: unknown;
}) {
  const Icon = KIND_META[kind].icon;
  if (items.length === 0) {
    return (
      <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-10 text-center text-sm">
        {projectAssetEmptyMessage(kind, source)}
      </p>
    );
  }

  return (
    <div
      role="list"
      className={
        kind === "skills" ? "space-y-3" : "overflow-hidden rounded-xl border"
      }
    >
      {items.map((item) => {
        const selected = item.id === selectedAssetId;
        const description = item.description?.trim();
        const toggleState = systemBindingToggleState(item, kind);
        const pending = bindingIntent?.assetId === item.id;
        const bindingBusy = bindingIntent !== null;
        const checked = pending ? bindingIntent.checked : toggleState.checked;
        const mcpUpdateAvailable =
          kind === "mcp-servers" && systemMcpBindingNeedsUpdate(item);
        const bindingItemError =
          bindingErrorAssetId === item.id && bindingError
            ? adminAssetErrorMessage(bindingError)
            : null;
        const projectToggleState =
          kind === "mcp-servers"
            ? projectMcpStatusToggleState(item)
            : projectSkillStatusToggleState(item);
        const projectStatusPending = projectStatusIntent?.assetId === item.id;
        const projectStatusBusy = projectStatusIntent !== null;
        const projectChecked = projectStatusPending
          ? projectStatusIntent.checked
          : projectToggleState.checked;
        const projectStatusItemError =
          projectStatusErrorAssetId === item.id && projectStatusError
            ? adminAssetErrorMessage(projectStatusError)
            : null;
        const error = bindingItemError ?? projectStatusItemError;
        return (
          <div
            key={item.id}
            role="listitem"
            className={`group flex flex-wrap items-stretch transition-colors ${
              kind === "skills"
                ? "overflow-hidden rounded-xl border"
                : "border-b last:border-b-0"
            } ${selected ? "bg-selection-subtle/60" : "hover:bg-muted/50"}`}
          >
            <button
              type="button"
              aria-haspopup="dialog"
              aria-expanded={selected}
              aria-label={`查看 ${item.display_name} 详情`}
              className={`focus-visible:ring-ring flex min-w-0 flex-1 items-center text-left focus-visible:ring-2 focus-visible:outline-none ${
                kind === "skills"
                  ? "gap-4 px-5 py-5"
                  : "gap-3 px-4 py-3 sm:px-4"
              }`}
              onClick={() => onSelect(item)}
            >
              {kind === "skills" ? (
                <>
                  <span className="min-w-0 flex-1">
                    <span className="flex min-w-0 items-center gap-2">
                      <span className="truncate text-base font-semibold">
                        {item.display_name}
                      </span>
                      {kind !== "skills" && item.status !== "active" ? (
                        <AssetStatusBadge status={item.status} />
                      ) : null}
                    </span>
                    <span className="text-muted-foreground mt-2 line-clamp-3 block text-sm leading-6">
                      {description && description.length > 0
                        ? description
                        : "暂无技能描述。"}
                    </span>
                  </span>
                </>
              ) : (
                <>
                  <span className="bg-muted flex size-9 shrink-0 items-center justify-center rounded-lg">
                    <Icon aria-hidden className="size-4.5" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex min-w-0 items-center gap-2">
                      <span className="truncate text-sm font-semibold">
                        {item.display_name}
                      </span>
                      {item.status !== "active" &&
                      !(
                        kind === "mcp-servers" &&
                        item.scope === "project" &&
                        item.status === "suspended"
                      ) ? (
                        <AssetStatusBadge status={item.status} />
                      ) : null}
                    </span>
                    <span className="text-muted-foreground mt-0.5 block truncate font-mono text-xs">
                      {item.slug}
                    </span>
                  </span>
                  {kind !== "mcp-servers" ? (
                    <>
                      <span className="hidden shrink-0 text-right md:block">
                        <span className="text-sm font-medium">
                          {assetAvailability(item, kind)}
                        </span>
                        <time className="text-muted-foreground mt-0.5 block text-xs">
                          {new Date(item.updated_at).toLocaleDateString(
                            "zh-CN",
                          )}
                        </time>
                      </span>
                      <ArrowRightIcon
                        aria-hidden
                        className="text-muted-foreground size-4 shrink-0 transition-transform group-hover:translate-x-0.5"
                      />
                    </>
                  ) : null}
                </>
              )}
            </button>

            {source === "system" ? (
              <div
                className={
                  kind === "skills"
                    ? "flex w-20 shrink-0 items-center justify-center px-4 py-5"
                    : "border-border/70 flex min-w-28 shrink-0 items-center justify-end gap-2 border-l px-3 sm:min-w-36 sm:px-4"
                }
              >
                {kind !== "skills" && kind !== "mcp-servers" ? (
                  <span className="text-muted-foreground hidden text-xs sm:inline">
                    {checked ? "已启用" : "未启用"}
                  </span>
                ) : null}
                {mcpUpdateAvailable ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={bindingBusy || !onSyncSystemMcpBinding}
                    aria-busy={pending || undefined}
                    aria-label={`更新 ${item.display_name} 配置`}
                    onClick={() => onSyncSystemMcpBinding?.(item)}
                  >
                    更新
                  </Button>
                ) : null}
                <Switch
                  checked={checked}
                  disabled={toggleState.disabled || bindingBusy}
                  className="data-[state=checked]:bg-success focus-visible:ring-selection/30"
                  aria-busy={pending || undefined}
                  aria-label={`${checked ? "停用" : "启用"} ${item.display_name}`}
                  title={
                    !toggleState.targetVersionId && !checked
                      ? kind === "mcp-servers"
                        ? "没有可启用的已发布配置"
                        : "没有可启用的当前版本"
                      : undefined
                  }
                  onCheckedChange={(next) => onToggleSystemBinding(item, next)}
                />
              </div>
            ) : kind === "skills" || kind === "mcp-servers" ? (
              <div
                className={
                  kind === "skills"
                    ? "flex min-w-28 shrink-0 flex-col items-center justify-center gap-1.5 px-5 py-5"
                    : "border-border/70 flex min-w-28 shrink-0 items-center justify-end border-l px-3 sm:min-w-36 sm:px-4"
                }
              >
                <Switch
                  checked={projectChecked}
                  disabled={
                    projectToggleState.disabled ||
                    projectStatusBusy ||
                    !onToggleProjectAssetStatus
                  }
                  className="data-[state=checked]:bg-success focus-visible:ring-selection/30"
                  aria-busy={projectStatusPending || undefined}
                  aria-label={`${projectChecked ? "停用" : "启用"} ${item.display_name}`}
                  title={projectToggleState.disabledReason ?? undefined}
                  onCheckedChange={(next) =>
                    onToggleProjectAssetStatus?.(item, next)
                  }
                />
                {kind === "skills" && projectToggleState.disabledReason ? (
                  <span className="text-muted-foreground text-xs whitespace-nowrap">
                    {projectToggleState.disabledReason}
                  </span>
                ) : null}
              </div>
            ) : null}

            {error ? (
              <p
                role="alert"
                className="text-destructive border-destructive/20 bg-destructive/5 basis-full border-t px-4 py-2 text-xs"
              >
                {error}
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

export function ProjectAssetListView({
  kind,
  data,
  source,
  selectedAssetId,
  onSelect,
  onToggleSystemBinding,
  onSyncSystemMcpBinding,
  onToggleProjectAssetStatus,
  bindingIntent,
  bindingErrorAssetId,
  bindingError,
  projectStatusIntent,
  projectStatusErrorAssetId,
  projectStatusError,
}: {
  kind: MutableAssetKind;
  data: ProjectAssetList;
  source: ProjectAssetSourceFilter;
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
  onToggleSystemBinding: (item: ProjectAssetItem, checked: boolean) => void;
  onSyncSystemMcpBinding?: (item: ProjectAssetItem) => void;
  onToggleProjectAssetStatus?: (
    item: ProjectAssetItem,
    checked: boolean,
  ) => void;
  bindingIntent?: { assetId: string; checked: boolean } | null;
  bindingErrorAssetId?: string | null;
  bindingError?: unknown;
  projectStatusIntent?: { assetId: string; checked: boolean } | null;
  projectStatusErrorAssetId?: string | null;
  projectStatusError?: unknown;
}) {
  return (
    <AssetList
      kind={kind}
      source={source}
      items={source === "system" ? data.system_items : data.project_items}
      selectedAssetId={selectedAssetId}
      onSelect={onSelect}
      onToggleSystemBinding={onToggleSystemBinding}
      onSyncSystemMcpBinding={onSyncSystemMcpBinding}
      onToggleProjectAssetStatus={onToggleProjectAssetStatus}
      bindingIntent={bindingIntent}
      bindingErrorAssetId={bindingErrorAssetId}
      bindingError={bindingError}
      projectStatusIntent={projectStatusIntent}
      projectStatusErrorAssetId={projectStatusErrorAssetId}
      projectStatusError={projectStatusError}
    />
  );
}

type ProjectAssetSelectionIntent = {
  assetId: string | null;
  href: string | null;
  history: "push" | "replace";
  routeAlreadyChanged: boolean;
};

type PendingProjectAssetSelection = {
  intent: ProjectAssetSelectionIntent;
  restoreHref: string | null;
};

function ProjectAssetCatalog({
  accountId,
  project,
  kind,
  layout,
  createOpen,
  initialSelectedAssetId,
  initialSelectedVersionId,
  initialFocusSkillSecrets,
  selectionQueryParam,
  selectionDependentQueryParams,
  onCreateOpenChange,
  renderList,
  renderDetailActions,
  renderAssetEditor,
  renderVersion,
  renderLead,
}: {
  accountId: string;
  project: Project;
  kind: MutableAssetKind;
  layout: ProjectAssetPageLayout;
  createOpen: boolean;
  initialSelectedAssetId: string | null;
  initialSelectedVersionId: string | null;
  initialFocusSkillSecrets: boolean;
  selectionQueryParam?: string;
  selectionDependentQueryParams: readonly string[];
  onCreateOpenChange: (open: boolean) => void;
  renderList?: (context: ProjectAssetListRenderContext) => ReactNode;
  renderDetailActions?: (context: {
    item: ProjectAssetItem;
    editing: boolean;
  }) => ReactNode;
  renderAssetEditor?: (
    effectiveVersion: AssetVersion | null,
    context: ProjectAssetVersionRenderContext,
  ) => ReactNode;
  renderVersion?: (
    version: AssetVersion,
    context: ProjectAssetVersionRenderContext,
  ) => ReactNode;
  renderLead?: (context: {
    project: Project;
    data: ProjectAssetList;
  }) => ReactNode;
}) {
  const { t } = useI18n();
  const pathname = usePathname();
  const router = useRouter();
  const query = useProjectAssets(accountId, project.id, kind);
  const createVersion = useCreateProjectAssetVersion(
    accountId,
    project.id,
    kind === "skills" ? kind : null,
  );
  const enableBinding = useEnableProjectSystemBinding(
    accountId,
    project.id,
    BINDING_KIND[kind],
  );
  const disableBinding = useDisableProjectSystemBinding(
    accountId,
    project.id,
    BINDING_KIND[kind],
  );
  const syncCurrentMcpBinding = useSyncCurrentProjectSystemMcpBinding(
    accountId,
    project.id,
  );
  const changeStatus = useChangeProjectAssetStatus(accountId, project.id, kind);
  const importSkill = useImportProjectSkillArchive(accountId, project.id);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(
    initialSelectedAssetId,
  );
  const [importOpen, setImportOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [sourceFilter, setSourceFilter] =
    useState<ProjectAssetSourceFilter>("project");
  const [sourceTouched, setSourceTouched] = useState(false);
  const [bindingIntent, setBindingIntent] = useState<{
    assetId: string;
    checked: boolean;
  } | null>(null);
  const [bindingAssetId, setBindingAssetId] = useState<string | null>(null);
  const [projectStatusIntent, setProjectStatusIntent] = useState<{
    assetId: string;
    checked: boolean;
  } | null>(null);
  const [versionAsset, setVersionAsset] = useState<ProjectAssetItem | null>(
    null,
  );
  const [mcpEditConfiguration, setMcpEditConfiguration] =
    useState<ProjectMcpEditableConfigurationResponse | null>(null);
  const versionDialogGeneration = useRef(0);
  const activeVersionDialog = useRef<VersionDialogSubmissionToken | null>(null);
  const [createdVersions, setCreatedVersions] = useState<
    Record<string, string>
  >(() =>
    initialSelectedAssetId && initialSelectedVersionId
      ? { [initialSelectedAssetId]: initialSelectedVersionId }
      : {},
  );
  const [importedSkillSelection, setImportedSkillSelection] =
    useState<ImportedSkillSelection | null>(null);
  const [secretSetupAssetId, setSecretSetupAssetId] = useState<string | null>(
    null,
  );
  const initialSecretSetupSelectionRef = useRef<ImportedSkillSelection | null>(
    initialFocusSkillSecrets &&
      kind === "skills" &&
      initialSelectedAssetId &&
      initialSelectedVersionId
      ? {
          assetId: initialSelectedAssetId,
          versionId: initialSelectedVersionId,
        }
      : null,
  );
  const appliedInitialVersionSelectionRef = useRef(
    initialSelectedAssetId && initialSelectedVersionId
      ? `${initialSelectedAssetId}:${initialSelectedVersionId}:${initialFocusSkillSecrets}`
      : null,
  );
  const [configuredMcpSelection, setConfiguredMcpSelection] =
    useState<ImportedSkillSelection | null>(null);
  const [configuredMcpStatus, setConfiguredMcpStatus] = useState<
    "published" | "pending_approval" | null
  >(null);
  const [versionSubmission, setVersionSubmission] = useState<{
    token: VersionDialogSubmissionToken;
    pending: boolean;
    error: unknown;
  } | null>(null);
  const [detailDirty, setDetailDirty] = useState(false);
  const [pendingSelection, setPendingSelection] =
    useState<PendingProjectAssetSelection | null>(null);
  const selectedAssetIdRef = useRef(selectedAssetId);
  const detailDirtyRef = useRef(detailDirty);
  const pendingSelectionRef = useRef(pendingSelection);
  // A popstate has already changed both the visible URL and Next's history
  // state before React can ask about discarding. Keep the last accepted pair
  // so the capture-phase listener can restore them as one unit.
  const currentSelectionHrefRef = useRef<string | null>(null);
  const currentSelectionHistoryStateRef = useRef<unknown>(undefined);
  const routeSyncTargetRef = useRef<{ assetId: string | null } | null>(null);
  const initialSelectedAssetIdRef = useRef(initialSelectedAssetId);
  selectedAssetIdRef.current = selectedAssetId;
  detailDirtyRef.current = detailDirty;
  pendingSelectionRef.current = pendingSelection;
  initialSelectedAssetIdRef.current = initialSelectedAssetId;

  const handleDetailDirtyChange = useCallback((dirty: boolean) => {
    detailDirtyRef.current = dirty;
    setDetailDirty(dirty);
  }, []);

  const applyAssetSelection = useCallback(
    (intent: ProjectAssetSelectionIntent) => {
      pendingSelectionRef.current = null;
      setPendingSelection(null);
      detailDirtyRef.current = false;
      setDetailDirty(false);
      selectedAssetIdRef.current = intent.assetId;
      setSelectedAssetId(intent.assetId);
      if (!selectionQueryParam || !intent.href) return;

      currentSelectionHrefRef.current = intent.href;
      routeSyncTargetRef.current =
        initialSelectedAssetIdRef.current === intent.assetId
          ? null
          : { assetId: intent.assetId };
      if (intent.history === "push") {
        router.push(intent.href, { scroll: false });
      } else {
        router.replace(intent.href, { scroll: false });
      }
    },
    [router, selectionQueryParam],
  );

  const requestAssetSelection = useCallback(
    (intent: ProjectAssetSelectionIntent) => {
      const currentAssetId = selectedAssetIdRef.current;
      const decision = projectAssetSelectionDecision(
        currentAssetId,
        intent.assetId,
        detailDirtyRef.current,
      );
      if (decision === "unchanged") {
        if (intent.routeAlreadyChanged && intent.href) {
          currentSelectionHrefRef.current = intent.href;
          currentSelectionHistoryStateRef.current = window.history.state;
        } else if (
          intent.href &&
          currentSelectionHrefRef.current !== intent.href
        ) {
          currentSelectionHrefRef.current = intent.href;
          if (intent.history === "push") {
            router.push(intent.href, { scroll: false });
          } else {
            router.replace(intent.href, { scroll: false });
          }
        }
        return;
      }
      if (decision === "apply") {
        applyAssetSelection(intent);
        return;
      }
      if (pendingSelectionRef.current) return;

      const restoreHref = currentSelectionHrefRef.current;
      const pending = { intent, restoreHref };
      pendingSelectionRef.current = pending;
      setPendingSelection(pending);
      if (intent.routeAlreadyChanged && restoreHref) {
        window.history.replaceState(
          currentSelectionHistoryStateRef.current === undefined
            ? window.history.state
            : currentSelectionHistoryStateRef.current,
          "",
          restoreHref,
        );
      }
    },
    [applyAssetSelection, router],
  );

  const cancelPendingSelection = useCallback(() => {
    const pending = pendingSelectionRef.current;
    if (!pending) return;
    pendingSelectionRef.current = null;
    setPendingSelection(null);
    if (pending.intent.routeAlreadyChanged && pending.restoreHref) {
      currentSelectionHrefRef.current = pending.restoreHref;
      routeSyncTargetRef.current =
        initialSelectedAssetIdRef.current === selectedAssetIdRef.current
          ? null
          : { assetId: selectedAssetIdRef.current };
      router.replace(pending.restoreHref, { scroll: false });
    }
  }, [router]);

  const confirmPendingSelection = useCallback(() => {
    const pending = pendingSelectionRef.current;
    if (!pending) return;
    applyAssetSelection(pending.intent);
  }, [applyAssetSelection]);

  useEffect(() => {
    if (!selectionQueryParam || currentSelectionHrefRef.current) return;
    currentSelectionHrefRef.current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    currentSelectionHistoryStateRef.current = window.history.state;
  }, [pathname, selectionQueryParam]);

  useEffect(() => {
    if (!selectionQueryParam) return;
    const routeSyncTarget = routeSyncTargetRef.current;
    if (routeSyncTarget) {
      if (routeSyncTarget.assetId === initialSelectedAssetId) {
        routeSyncTargetRef.current = null;
        currentSelectionHrefRef.current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        currentSelectionHistoryStateRef.current = window.history.state;
      }
      return;
    }
    if (pendingSelectionRef.current) return;
    if (initialSelectedAssetId === selectedAssetIdRef.current) {
      if (selectionQueryParam) {
        currentSelectionHrefRef.current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        currentSelectionHistoryStateRef.current = window.history.state;
      }
      return;
    }

    const href = selectionQueryParam
      ? `${window.location.pathname}${window.location.search}${window.location.hash}`
      : null;
    requestAssetSelection({
      assetId: initialSelectedAssetId,
      href,
      history: "replace",
      routeAlreadyChanged: Boolean(selectionQueryParam),
    });
  }, [
    initialSelectedAssetId,
    requestAssetSelection,
    selectedAssetId,
    selectionQueryParam,
  ]);

  useEffect(() => {
    if (!initialSelectedAssetId || !initialSelectedVersionId) {
      appliedInitialVersionSelectionRef.current = null;
      initialSecretSetupSelectionRef.current = null;
      return;
    }
    const identity = `${initialSelectedAssetId}:${initialSelectedVersionId}:${initialFocusSkillSecrets}`;
    if (appliedInitialVersionSelectionRef.current === identity) return;
    appliedInitialVersionSelectionRef.current = identity;
    setCreatedVersions((current) =>
      rememberRequestedVersion(
        current,
        initialSelectedAssetId,
        initialSelectedVersionId,
      ),
    );
    initialSecretSetupSelectionRef.current =
      initialFocusSkillSecrets && kind === "skills"
        ? {
            assetId: initialSelectedAssetId,
            versionId: initialSelectedVersionId,
          }
        : null;
  }, [
    initialFocusSkillSecrets,
    initialSelectedAssetId,
    initialSelectedVersionId,
    kind,
  ]);

  useEffect(() => {
    if (!selectionQueryParam) return;
    const syncSelectionFromHistory = () => {
      requestAssetSelection({
        assetId: projectAssetSelectionFromSearch(
          window.location.search,
          selectionQueryParam,
        ),
        href: `${window.location.pathname}${window.location.search}${window.location.hash}`,
        history: "replace",
        routeAlreadyChanged: true,
      });
    };
    window.addEventListener("popstate", syncSelectionFromHistory, true);
    return () =>
      window.removeEventListener("popstate", syncSelectionFromHistory, true);
  }, [requestAssetSelection, selectionQueryParam]);

  const navigateToAsset = useCallback(
    (assetId: string | null, history: "push" | "replace") => {
      requestAssetSelection({
        assetId,
        href: selectionQueryParam
          ? projectAssetSelectionHref(
              pathname,
              window.location.search,
              selectionQueryParam,
              assetId,
              selectionDependentQueryParams,
            )
          : null,
        history,
        routeAlreadyChanged: false,
      });
    },
    [
      pathname,
      requestAssetSelection,
      selectionDependentQueryParams,
      selectionQueryParam,
    ],
  );

  const navigateToSkillVersion = useCallback(
    (
      assetId: string,
      versionId: string,
      configureSecrets: boolean,
      history: "push" | "replace",
    ) => {
      requestAssetSelection({
        assetId,
        href: projectSkillExactVersionSelectionHref(
          pathname,
          window.location.search,
          assetId,
          versionId,
          configureSecrets,
        ),
        history,
        routeAlreadyChanged: false,
      });
    },
    [pathname, requestAssetSelection],
  );

  const handleRequestedVersionHandled = useCallback(
    (assetId: string, versionId: string, available: boolean) => {
      const secretSetupSelection = initialSecretSetupSelectionRef.current;
      if (
        available &&
        secretSetupSelection?.assetId === assetId &&
        secretSetupSelection.versionId === versionId
      ) {
        initialSecretSetupSelectionRef.current = null;
        setSecretSetupAssetId(assetId);
      } else if (
        !available &&
        secretSetupSelection?.assetId === assetId &&
        secretSetupSelection.versionId === versionId
      ) {
        initialSecretSetupSelectionRef.current = null;
        setSecretSetupAssetId((current) =>
          current === assetId ? null : current,
        );
      }
      setCreatedVersions((current) =>
        handleRequestedVersion(current, assetId, versionId),
      );
      if (
        available ||
        !selectionQueryParam ||
        selectedAssetIdRef.current !== assetId
      ) {
        return;
      }
      const href = projectAssetSelectionHref(
        pathname,
        window.location.search,
        selectionQueryParam,
        assetId,
        selectionDependentQueryParams,
      );
      currentSelectionHrefRef.current = href;
      router.replace(href, { scroll: false });
    },
    [pathname, router, selectionDependentQueryParams, selectionQueryParam],
  );

  const rememberVersion = useCallback((assetId: string, versionId: string) => {
    setCreatedVersions((current) =>
      rememberRequestedVersion(current, assetId, versionId),
    );
  }, []);

  function openVersionDialog(
    item: ProjectAssetItem,
    _selectedVersion: AssetVersion | null,
    editableMcpConfiguration?: ProjectMcpEditableConfigurationResponse,
  ) {
    if (kind === "agents") return;
    if (kind === "mcp-servers" && !editableMcpConfiguration) return;
    const token = {
      assetId: item.id,
      generation: ++versionDialogGeneration.current,
    };
    activeVersionDialog.current = token;
    createVersion.reset();
    setVersionSubmission(null);
    setVersionAsset(item);
    setMcpEditConfiguration(editableMcpConfiguration ?? null);
  }

  function closeVersionDialog() {
    const token = activeVersionDialog.current;
    activeVersionDialog.current = null;
    createVersion.reset();
    setVersionAsset(null);
    setMcpEditConfiguration(null);
    setVersionSubmission((current) =>
      current && token && versionDialogSubmissionMatches(current.token, token)
        ? null
        : current,
    );
  }

  async function submitVersion(input: VersionAuthoringInput) {
    const target = versionAsset;
    const token = activeVersionDialog.current;
    if (!target || token?.assetId !== target.id) return;
    setVersionSubmission({
      token,
      pending: true,
      error: null,
    });
    try {
      const result = await createVersion.mutateAsync({
        assetId: target.id,
        input,
      });
      const active = activeVersionDialog.current;
      if (!versionDialogSubmissionMatches(active, token)) return;
      activeVersionDialog.current = null;
      rememberVersion(target.id, result.data.id);
      setVersionAsset((current) =>
        closeCompletedVersionDialog(current, active, token),
      );
      setVersionSubmission((current) =>
        current && versionDialogSubmissionMatches(current.token, token)
          ? null
          : current,
      );
    } catch (error) {
      setVersionSubmission((current) =>
        current &&
        versionDialogSubmissionMatches(activeVersionDialog.current, token) &&
        versionDialogSubmissionMatches(current.token, token)
          ? { token, pending: false, error }
          : current,
      );
    }
  }

  const data = query.data;
  useEffect(() => {
    if (!sourceTouched && data) {
      setSourceFilter(
        kind === "agents" ? "project" : defaultProjectAssetSource(data),
      );
    }
  }, [data, kind, sourceTouched]);
  const filteredData = useMemo(
    () =>
      data
        ? {
            ...data,
            system_items: filterProjectAssetItems(data, searchQuery, "system"),
            project_items: filterProjectAssetItems(
              data,
              searchQuery,
              "project",
            ),
          }
        : undefined,
    [data, searchQuery],
  );
  const selectedItem = useMemo(() => {
    if (!data || !selectedAssetId) return null;
    return (
      [...data.system_items, ...data.project_items].find(
        (item) => item.id === selectedAssetId,
      ) ?? null
    );
  }, [data, selectedAssetId]);
  const systemSkillBindingItem = useMemo(() => {
    if (kind !== "skills" || !data || !bindingAssetId) return null;
    return data.system_items.find((item) => item.id === bindingAssetId) ?? null;
  }, [bindingAssetId, data, kind]);

  useEffect(() => {
    const readySelection = importedSkillSelectionReady(
      data,
      importedSkillSelection,
    );
    if (!readySelection) return;
    setCreatedVersions((current) =>
      rememberRequestedVersion(
        current,
        readySelection.assetId,
        readySelection.versionId,
      ),
    );
    setImportedSkillSelection(null);
    navigateToSkillVersion(
      readySelection.assetId,
      readySelection.versionId,
      secretSetupAssetId === readySelection.assetId,
      "push",
    );
  }, [
    secretSetupAssetId,
    data,
    importedSkillSelection,
    navigateToSkillVersion,
  ]);

  useEffect(() => {
    const readySelection = configuredMcpSelectionReady(
      data,
      configuredMcpSelection,
    );
    if (!readySelection) return;
    setSelectedAssetId(readySelection.assetId);
    setCreatedVersions((current) =>
      rememberRequestedVersion(
        current,
        readySelection.assetId,
        readySelection.versionId,
      ),
    );
    setConfiguredMcpSelection(null);
  }, [configuredMcpSelection, data]);

  useEffect(() => {
    if (selectedAssetId && data && !selectedItem) {
      navigateToAsset(null, "replace");
    }
  }, [data, navigateToAsset, selectedAssetId, selectedItem]);

  useEffect(() => {
    if (bindingAssetId && data && !systemSkillBindingItem) {
      setBindingAssetId(null);
    }
  }, [bindingAssetId, data, systemSkillBindingItem]);

  function runSystemBindingAction(
    item: ProjectAssetItem,
    checked: boolean,
    syncCurrent = false,
  ) {
    const action = projectSystemBindingListAction(
      kind,
      item,
      checked,
      syncCurrent,
    );
    if (!action) return;
    enableBinding.reset();
    disableBinding.reset();
    syncCurrentMcpBinding.reset();
    setBindingIntent({ assetId: item.id, checked });
    const settle = () =>
      setBindingIntent((current) =>
        current?.assetId === item.id ? null : current,
      );

    if (action.type === "sync-current") {
      syncCurrentMcpBinding.mutate(
        { assetId: action.assetId, input: action.input },
        { onSettled: settle },
      );
      return;
    }
    if (action.type === "enable") {
      enableBinding.mutate(action.input, { onSettled: settle });
      return;
    }
    if (action.type === "disable") {
      disableBinding.mutate(
        { assetId: action.assetId, input: action.input },
        { onSettled: settle },
      );
    }
  }

  function toggleSystemBinding(item: ProjectAssetItem, checked: boolean) {
    if (kind === "skills" && checked && item.binding?.enabled !== true) {
      if (
        projectSystemSkillBindingCanManage(
          item,
          project.capabilities.includes("shared_assets.manage_bindings"),
        )
      ) {
        setBindingAssetId(item.id);
      }
      return;
    }
    runSystemBindingAction(item, checked);
  }

  function syncSystemMcpBinding(item: ProjectAssetItem) {
    runSystemBindingAction(item, true, true);
  }

  function toggleProjectAssetStatus(item: ProjectAssetItem, checked: boolean) {
    if (kind !== "skills" && kind !== "mcp-servers") return;
    const state =
      kind === "mcp-servers"
        ? projectMcpStatusToggleState(item)
        : projectSkillStatusToggleState(item);
    if (state.disabled || state.checked === checked) return;

    changeStatus.reset();
    setProjectStatusIntent({ assetId: item.id, checked });
    changeStatus.mutate(
      {
        assetId: item.id,
        action:
          kind === "skills"
            ? checked
              ? "enable"
              : "suspend"
            : checked
              ? "activate"
              : "suspend",
        input:
          kind === "skills"
            ? { expected_revision: item.revision }
            : { expected_asset_version: item.revision },
      },
      {
        onError: (error) => {
          if (kind === "skills" && projectSkillSecretSetupRequired(error)) {
            setSecretSetupAssetId(item.id);
            const currentVersionId = item.current_version_id;
            if (currentVersionId) {
              setCreatedVersions((current) =>
                rememberRequestedVersion(current, item.id, currentVersionId),
              );
              navigateToSkillVersion(item.id, currentVersionId, true, "push");
            } else {
              navigateToAsset(item.id, "push");
            }
          }
        },
        onSettled: () =>
          setProjectStatusIntent((current) =>
            current?.assetId === item.id ? null : current,
          ),
      },
    );
  }

  async function submitProjectSkillArchive(archive: File) {
    try {
      const result = await importSkill.mutateAsync(archive);
      setImportOpen(false);
      setSourceTouched(true);
      setSourceFilter("project");
      setImportedSkillSelection({
        assetId: result.item.id,
        versionId: result.version.id,
      });
      setSecretSetupAssetId(
        projectSkillImportNeedsSecretSetup(result.version)
          ? result.item.id
          : null,
      );
    } catch {
      // The dialog renders the mutation's mapped public error.
    }
  }

  if (query.isLoading) {
    return (
      <div className="space-y-4" aria-label="正在加载资产">
        <Skeleton className="h-10 w-full rounded-lg" />
        <Skeleton className="h-44 w-full rounded-xl" />
      </div>
    );
  }

  if (query.error || !data || !filteredData) {
    return (
      <div className="border-destructive/30 rounded-2xl border p-6">
        <p role="alert" className="text-destructive text-sm">
          {projectAssetListErrorMessage(kind, query.error)}
        </p>
        <Button
          type="button"
          className="mt-4"
          variant="outline"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          {query.isFetching ? "重试中…" : "重试"}
        </Button>
      </div>
    );
  }

  const canCreate = project.capabilities.includes("shared_assets.edit");
  const visibleCount =
    sourceFilter === "system"
      ? filteredData.system_items.length
      : filteredData.project_items.length;
  const sourceCount =
    sourceFilter === "system"
      ? data.system_items.length
      : data.project_items.length;
  const filterActive = searchQuery.trim() !== "";
  const sourceOptions = projectAssetSourceOptions(kind);
  const bindingError =
    syncCurrentMcpBinding.error ?? enableBinding.error ?? disableBinding.error;
  const bindingErrorAssetId = syncCurrentMcpBinding.error
    ? syncCurrentMcpBinding.variables?.assetId
    : enableBinding.error
      ? enableBinding.variables?.asset_id
      : disableBinding.error
        ? disableBinding.variables?.assetId
        : null;
  const projectStatusErrorAssetId = changeStatus.error
    ? changeStatus.variables?.assetId
    : null;
  return (
    <>
      {renderLead?.({ project, data })}

      {kind === "mcp-servers" && configuredMcpStatus ? (
        <p
          role="status"
          aria-live="polite"
          className="border-success/30 bg-success/5 text-success mb-4 rounded-xl border px-4 py-3 text-sm"
        >
          {configuredMcpSuccessMessage(configuredMcpStatus)}
        </p>
      ) : null}

      {layout === "agent-cards" && renderList ? (
        renderList({
          project,
          data,
          items: data.project_items,
          source: "project",
          selectedAssetId,
          onSelect: (item) => navigateToAsset(item.id, "push"),
        })
      ) : (
        <Tabs
          value={sourceFilter}
          onValueChange={(value) => {
            setSourceTouched(true);
            setSourceFilter(value as ProjectAssetSourceFilter);
          }}
          className="gap-0"
        >
          <div className="flex flex-col gap-3 border-b pb-4 lg:flex-row lg:items-center">
            {sourceOptions.length > 1 ? (
              <TabsList
                variant="line"
                aria-label="资产来源"
                className="w-full justify-start lg:w-auto"
              >
                {sourceOptions.map(([value, label]) => {
                  const count =
                    value === "system"
                      ? data.system_items.length
                      : data.project_items.length;
                  return (
                    <TabsTrigger
                      key={value}
                      value={value}
                      className="data-[state=active]:after:bg-selection min-w-0 flex-1 px-3 lg:flex-none"
                    >
                      {label}
                      <span className="text-muted-foreground text-xs tabular-nums">
                        {count}
                      </span>
                    </TabsTrigger>
                  );
                })}
              </TabsList>
            ) : null}

            <label
              className={`relative min-w-0 flex-1 ${
                sourceOptions.length > 1 ? "lg:ml-2" : ""
              }`}
            >
              <span className="sr-only">搜索{KIND_META[kind].singular}</span>
              <SearchIcon
                aria-hidden
                className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                type="search"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                placeholder="搜索名称或 slug"
                className="h-9 bg-transparent pl-9"
              />
            </label>

            {filterActive ? (
              <p
                role="status"
                className="text-muted-foreground shrink-0 text-xs tabular-nums"
              >
                显示 {visibleCount} / {sourceCount} 项
              </p>
            ) : null}

            {canCreate && sourceFilter === "project" ? (
              <div className="flex shrink-0 items-center gap-2">
                {kind === "skills" ? (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button type="button" size="sm">
                        <PlusIcon aria-hidden className="size-4" />
                        新建 Skill
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <SkillCreateMenuItems
                        projectSlug={project.slug}
                        onImport={() => {
                          importSkill.reset();
                          setImportOpen(true);
                        }}
                      />
                    </DropdownMenuContent>
                  </DropdownMenu>
                ) : (
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => {
                      if (kind === "mcp-servers") {
                        setConfiguredMcpStatus(null);
                      }
                      onCreateOpenChange(true);
                    }}
                  >
                    <PlusIcon aria-hidden className="size-4" />
                    {projectAssetPrimaryActionLabel(kind)}
                  </Button>
                )}
              </div>
            ) : null}
          </div>

          {sourceOptions.map(([value]) => {
            const items =
              value === "system"
                ? filteredData.system_items
                : filteredData.project_items;
            return (
              <TabsContent key={value} value={value} className="pt-4">
                {filterActive && items.length === 0 ? (
                  <p
                    role="status"
                    className="text-muted-foreground rounded-xl border border-dashed px-5 py-10 text-center text-sm"
                  >
                    没有找到匹配的 {KIND_META[kind].singular}，请调整搜索词。
                  </p>
                ) : renderList ? (
                  renderList({
                    project,
                    data: filteredData,
                    items,
                    source: value,
                    selectedAssetId,
                    onSelect: (item) => navigateToAsset(item.id, "push"),
                  })
                ) : (
                  <ProjectAssetListView
                    kind={kind}
                    data={filteredData}
                    source={value}
                    selectedAssetId={selectedAssetId}
                    onSelect={(item) => navigateToAsset(item.id, "push")}
                    onToggleSystemBinding={toggleSystemBinding}
                    onSyncSystemMcpBinding={syncSystemMcpBinding}
                    onToggleProjectAssetStatus={toggleProjectAssetStatus}
                    bindingIntent={bindingIntent}
                    bindingErrorAssetId={bindingErrorAssetId}
                    bindingError={bindingError}
                    projectStatusIntent={projectStatusIntent}
                    projectStatusErrorAssetId={projectStatusErrorAssetId}
                    projectStatusError={changeStatus.error}
                  />
                )}
              </TabsContent>
            );
          })}
        </Tabs>
      )}

      {selectedItem && (
        <ProjectAssetDetailSheet
          key={`${kind}:${selectedItem.id}`}
          accountId={accountId}
          projectId={project.id}
          projectCapabilities={project.capabilities}
          kind={kind}
          item={selectedItem}
          open
          requestedVersionId={createdVersions[selectedItem.id] ?? null}
          onOpenChange={(next) => !next && navigateToAsset(null, "replace")}
          onCreateVersion={openVersionDialog}
          onDeleted={(assetId) => {
            if (selectedAssetId === assetId) {
              navigateToAsset(null, "replace");
            }
            setCreatedVersions((current) => {
              if (!(assetId in current)) return current;
              const next = { ...current };
              delete next[assetId];
              return next;
            });
          }}
          onDirtyChange={handleDetailDirtyChange}
          onVersionCreated={rememberVersion}
          onRequestedVersionHandled={handleRequestedVersionHandled}
          renderDetailActions={renderDetailActions}
          renderAssetEditor={renderAssetEditor}
          renderVersion={renderVersion}
          focusSkillSecrets={secretSetupAssetId === selectedItem.id}
          onSkillSecretsFocused={() =>
            setSecretSetupAssetId((current) =>
              current === selectedItem.id ? null : current,
            )
          }
          onSkillSecretSetupRequired={(versionId) => {
            setSecretSetupAssetId(selectedItem.id);
            if (!versionId) return;
            setCreatedVersions((current) =>
              rememberRequestedVersion(current, selectedItem.id, versionId),
            );
            navigateToSkillVersion(selectedItem.id, versionId, true, "replace");
          }}
        />
      )}

      <Dialog
        open={pendingSelection !== null}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) cancelPendingSelection();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              放弃当前 {KIND_META[kind].singular} 的未保存修改？
            </DialogTitle>
            <DialogDescription>
              {pendingSelection?.intent.assetId
                ? `确认后将切换到另一个 ${KIND_META[kind].singular}，当前修改不会保存。`
                : "确认后将关闭详情，当前修改不会保存。"}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={cancelPendingSelection}
            >
              继续编辑
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={confirmPendingSelection}
            >
              放弃修改并继续
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {kind === "skills" && systemSkillBindingItem ? (
        <SystemBindingDialog
          accountId={accountId}
          projectId={project.id}
          kind="skills"
          item={systemSkillBindingItem}
          open
          onOpenChange={(nextOpen) => {
            if (!nextOpen) setBindingAssetId(null);
          }}
          onConflict={() => void query.refetch()}
        />
      ) : null}

      {kind === "mcp-servers" ? (
        <ProjectMcpCreateDialog
          accountId={accountId}
          project={project}
          open={createOpen}
          onOpenChange={onCreateOpenChange}
          onCompleted={({ assetId, versionId, status }) => {
            setSourceTouched(true);
            setSourceFilter("project");
            setConfiguredMcpSelection({ assetId, versionId });
            setConfiguredMcpStatus(status);
          }}
        />
      ) : null}

      {kind === "skills" ? (
        <ProjectSkillImportDialog
          open={importOpen}
          pending={importSkill.isPending}
          errorMessage={
            importSkill.error
              ? projectSkillImportErrorMessage(importSkill.error)
              : null
          }
          onOpenChange={(next) => {
            if (!next) importSkill.reset();
            setImportOpen(next);
          }}
          onSelectionChange={() => importSkill.reset()}
          onSubmit={(archive) => void submitProjectSkillArchive(archive)}
        />
      ) : null}

      {versionAsset && kind === "skills" ? (
        <CreateVersionDialog
          key={`${versionAsset.id}:${activeVersionDialog.current?.generation ?? "closed"}`}
          kind="skills"
          asset={versionAsset}
          open
          pending={
            versionSubmission !== null &&
            versionDialogSubmissionMatches(
              activeVersionDialog.current,
              versionSubmission.token,
            ) &&
            versionSubmission.pending
          }
          errorMessage={
            versionSubmission !== null &&
            versionDialogSubmissionMatches(
              activeVersionDialog.current,
              versionSubmission.token,
            ) &&
            versionSubmission.error
              ? adminAssetErrorMessage(
                  versionSubmission.error,
                  t.adminAssets.errors,
                )
              : null
          }
          onOpenChange={(next) => !next && closeVersionDialog()}
          onSubmit={(input: VersionAuthoringInput) => void submitVersion(input)}
        />
      ) : null}

      {versionAsset && kind === "mcp-servers" && mcpEditConfiguration ? (
        <ProjectMcpEditDialog
          key={`${versionAsset.id}:${mcpEditConfiguration.version.id}:${mcpEditConfiguration.item.version}:${activeVersionDialog.current?.generation ?? "closed"}`}
          accountId={accountId}
          project={project}
          configuration={mcpEditConfiguration}
          open
          onOpenChange={(next) => !next && closeVersionDialog()}
          onCompleted={({ assetId, versionId }) => {
            const token = activeVersionDialog.current;
            if (token?.assetId !== assetId) return;
            activeVersionDialog.current = null;
            rememberVersion(assetId, versionId);
            setVersionAsset(null);
            setMcpEditConfiguration(null);
            setVersionSubmission(null);
          }}
        />
      ) : null}
    </>
  );
}

export function ProjectAssetPageShell({
  kind,
  title,
  layout = "default",
  headerActions,
  initialSelectedAssetId = null,
  initialSelectedVersionId = null,
  initialFocusSkillSecrets = false,
  selectionQueryParam,
  selectionDependentQueryParams = [],
  renderList,
  renderDetailActions,
  renderAssetEditor,
  renderVersion,
  renderLead,
}: {
  kind: MutableAssetKind;
  title: string;
  layout?: ProjectAssetPageLayout;
  headerActions?: ReactNode;
  initialSelectedAssetId?: string | null;
  initialSelectedVersionId?: string | null;
  initialFocusSkillSecrets?: boolean;
  selectionQueryParam?: string;
  selectionDependentQueryParams?: readonly string[];
  renderList?: (context: ProjectAssetListRenderContext) => ReactNode;
  renderDetailActions?: (context: {
    item: ProjectAssetItem;
    editing: boolean;
  }) => ReactNode;
  renderAssetEditor?: (
    effectiveVersion: AssetVersion | null,
    context: ProjectAssetVersionRenderContext,
  ) => ReactNode;
  renderVersion?: (
    version: AssetVersion,
    context: ProjectAssetVersionRenderContext,
  ) => ReactNode;
  renderLead?: (context: {
    project: Project;
    data: ProjectAssetList;
  }) => ReactNode;
}) {
  const { user } = useAuth();
  const project = useCurrentProject();
  const [createOpen, setCreateOpen] = useState(false);

  if (!user) return null;

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
      <ProjectPageHeader
        className="mb-5"
        title={title}
        actions={
          headerActions ||
          (layout === "agent-cards" &&
            agentBuilderCanAuthor(project.capabilities)) ? (
            <div className="flex flex-wrap items-center justify-end gap-2">
              {headerActions}
              {layout === "agent-cards" &&
              agentBuilderCanAuthor(project.capabilities) ? (
                <Button asChild>
                  <Link
                    href={`/projects/${encodeURIComponent(project.slug)}/agents/new`}
                  >
                    <PlusIcon aria-hidden className="size-4" />
                    新建 Agent
                  </Link>
                </Button>
              ) : null}
            </div>
          ) : null
        }
      />

      <ProjectAssetCatalog
        accountId={user.id}
        project={project}
        kind={kind}
        layout={layout}
        createOpen={createOpen}
        initialSelectedAssetId={initialSelectedAssetId}
        initialSelectedVersionId={initialSelectedVersionId}
        initialFocusSkillSecrets={initialFocusSkillSecrets}
        selectionQueryParam={selectionQueryParam}
        selectionDependentQueryParams={selectionDependentQueryParams}
        onCreateOpenChange={setCreateOpen}
        renderList={renderList}
        renderDetailActions={renderDetailActions}
        renderAssetEditor={renderAssetEditor}
        renderVersion={renderVersion}
        renderLead={renderLead}
      />
    </main>
  );
}
