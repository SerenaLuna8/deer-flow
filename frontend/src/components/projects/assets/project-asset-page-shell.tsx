"use client";

import {
  ArrowRightIcon,
  BotIcon,
  FilePlus2Icon,
  PlugZapIcon,
  PlusIcon,
  SearchIcon,
  SparklesIcon,
  UploadIcon,
} from "lucide-react";
import Link from "next/link";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  CreateAssetDialog,
  CreateVersionDialog,
  type VersionAuthoringInput,
} from "@/components/admin/assets/admin-asset-dialogs";
import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { Button } from "@/components/ui/button";
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
import type { Project } from "@/core/projects/types";
import {
  useCreateProjectAsset,
  useCreateProjectAssetVersion,
  useChangeProjectAssetStatus,
  useDisableProjectSystemBinding,
  useEnableProjectSystemBinding,
  useImportProjectSkillArchive,
  useProjectAssets,
  type AssetKind,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

import { useCurrentProject } from "../project-context";
import { ProjectPageHeader } from "../project-page-header";

import { ProjectAssetDetailSheet } from "./project-asset-detail-sheet";
import type { ProjectAssetVersionRenderContext } from "./project-asset-detail-sheet";
import {
  projectAssetCreateErrorMessage,
  projectSkillStatusToggleState,
} from "./project-asset-view-model";
import {
  ProjectSkillImportDialog,
  projectSkillImportErrorMessage,
} from "./project-skill-import-dialog";

type MutableAssetKind = Exclude<AssetListKind, "credentials">;
export type ProjectAssetSourceFilter = "system" | "project";
export type ProjectAssetPageLayout = "default" | "agent-cards";
export type ProjectAssetListRenderContext = {
  project: Project;
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
  onBlank,
  onImport,
}: {
  projectSlug: string;
  onBlank: () => void;
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
      <DropdownMenuItem onSelect={onBlank}>
        <FilePlus2Icon aria-hidden className="size-4" />
        从空白创建
      </DropdownMenuItem>
      <DropdownMenuItem onSelect={onImport}>
        <UploadIcon aria-hidden className="size-4" />
        上传压缩包
      </DropdownMenuItem>
    </>
  );
}

function assetAvailability(item: ProjectAssetItem): string {
  if (item.scope === "system") {
    if (!item.binding) return "未启用";
    if (!item.binding.enabled) return "已从项目停用";
    if (
      item.current_published_version_id &&
      item.current_published_version_id !== item.binding.version_id
    ) {
      return "有新版本";
    }
    return "已在项目启用";
  }
  return item.current_published_version_id ? "已有发布版本" : "尚未发布";
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

export function systemBindingToggleState(item: ProjectAssetItem): {
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
      ? (item.binding?.version_id ?? item.current_published_version_id)
      : null;
  return {
    checked,
    disabled: !canManage || (!checked && targetVersionId === null),
    targetVersionId,
  };
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
  onToggleProjectSkillStatus,
  bindingIntent,
  bindingErrorAssetId,
  bindingError,
  skillStatusIntent,
  skillStatusErrorAssetId,
  skillStatusError,
}: {
  kind: MutableAssetKind;
  source: ProjectAssetSourceFilter;
  items: ProjectAssetItem[];
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
  onToggleSystemBinding: (item: ProjectAssetItem, checked: boolean) => void;
  onToggleProjectSkillStatus?: (
    item: ProjectAssetItem,
    checked: boolean,
  ) => void;
  bindingIntent?: { assetId: string; checked: boolean } | null;
  bindingErrorAssetId?: string | null;
  bindingError?: unknown;
  skillStatusIntent?: { assetId: string; checked: boolean } | null;
  skillStatusErrorAssetId?: string | null;
  skillStatusError?: unknown;
}) {
  const Icon = KIND_META[kind].icon;
  const sourceLabel = source === "system" ? "系统提供" : "项目自建";

  if (items.length === 0) {
    return (
      <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-10 text-center text-sm">
        暂无{sourceLabel}的 {KIND_META[kind].singular}。
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
        const toggleState = systemBindingToggleState(item);
        const pending = bindingIntent?.assetId === item.id;
        const bindingBusy = bindingIntent !== null;
        const checked = pending ? bindingIntent.checked : toggleState.checked;
        const bindingItemError =
          bindingErrorAssetId === item.id && bindingError
            ? adminAssetErrorMessage(bindingError)
            : null;
        const skillToggleState = projectSkillStatusToggleState(item);
        const skillStatusPending = skillStatusIntent?.assetId === item.id;
        const skillStatusBusy = skillStatusIntent !== null;
        const skillChecked = skillStatusPending
          ? skillStatusIntent.checked
          : skillToggleState.checked;
        const skillStatusItemError =
          skillStatusErrorAssetId === item.id && skillStatusError
            ? adminAssetErrorMessage(skillStatusError)
            : null;
        const error = bindingItemError ?? skillStatusItemError;
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
              aria-label={`查看 ${item.display_name} 详情`}
              className={`focus-visible:ring-ring flex min-w-0 flex-1 items-center text-left focus-visible:ring-2 focus-visible:outline-none ${
                kind === "skills"
                  ? "gap-4 px-5 py-5"
                  : "gap-3 px-4 py-3 sm:px-4"
              }`}
              onClick={() => onSelect(item)}
            >
              {kind === "skills" ? (
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
                      {item.status !== "active" ? (
                        <AssetStatusBadge status={item.status} />
                      ) : null}
                    </span>
                    <span className="text-muted-foreground mt-0.5 block truncate font-mono text-xs">
                      {item.slug}
                    </span>
                  </span>
                  <span className="hidden shrink-0 text-right md:block">
                    <span className="text-sm font-medium">
                      {assetAvailability(item)}
                    </span>
                    <time className="text-muted-foreground mt-0.5 block text-xs">
                      {new Date(item.updated_at).toLocaleDateString("zh-CN")}
                    </time>
                  </span>
                  <ArrowRightIcon
                    aria-hidden
                    className="text-muted-foreground size-4 shrink-0 transition-transform group-hover:translate-x-0.5"
                  />
                </>
              )}
            </button>

            {source === "system" ? (
              <div
                className={
                  kind === "skills"
                    ? "flex min-w-20 shrink-0 items-center justify-center px-5 py-5"
                    : "border-border/70 flex min-w-28 shrink-0 items-center justify-end gap-2 border-l px-3 sm:min-w-36 sm:px-4"
                }
              >
                {kind !== "skills" && kind !== "mcp-servers" ? (
                  <span className="text-muted-foreground hidden text-xs sm:inline">
                    {checked ? "已启用" : "未启用"}
                  </span>
                ) : null}
                {kind === "mcp-servers" ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => onSelect(item)}
                  >
                    管理绑定
                  </Button>
                ) : (
                  <Switch
                    checked={checked}
                    disabled={toggleState.disabled || bindingBusy}
                    className="data-[state=checked]:bg-success focus-visible:ring-selection/30"
                    aria-busy={pending || undefined}
                    aria-label={`${checked ? "停用" : "启用"} ${item.display_name}`}
                    title={
                      !toggleState.targetVersionId && !checked
                        ? "没有可启用的已发布版本"
                        : undefined
                    }
                    onCheckedChange={(next) =>
                      onToggleSystemBinding(item, next)
                    }
                  />
                )}
              </div>
            ) : kind === "skills" ? (
              <div className="flex min-w-28 shrink-0 flex-col items-center justify-center gap-1.5 px-5 py-5">
                <Switch
                  checked={skillChecked}
                  disabled={
                    skillToggleState.disabled ||
                    skillStatusBusy ||
                    !onToggleProjectSkillStatus
                  }
                  className="data-[state=checked]:bg-success focus-visible:ring-selection/30"
                  aria-busy={skillStatusPending || undefined}
                  aria-label={`${skillChecked ? "停用" : "启用"} ${item.display_name}`}
                  title={skillToggleState.disabledReason ?? undefined}
                  onCheckedChange={(next) =>
                    onToggleProjectSkillStatus?.(item, next)
                  }
                />
                {skillToggleState.disabledReason ? (
                  <span className="text-muted-foreground text-xs whitespace-nowrap">
                    {skillToggleState.disabledReason}
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
  onToggleProjectSkillStatus,
  bindingIntent,
  bindingErrorAssetId,
  bindingError,
  skillStatusIntent,
  skillStatusErrorAssetId,
  skillStatusError,
}: {
  kind: MutableAssetKind;
  data: ProjectAssetList;
  source: ProjectAssetSourceFilter;
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
  onToggleSystemBinding: (item: ProjectAssetItem, checked: boolean) => void;
  onToggleProjectSkillStatus?: (
    item: ProjectAssetItem,
    checked: boolean,
  ) => void;
  bindingIntent?: { assetId: string; checked: boolean } | null;
  bindingErrorAssetId?: string | null;
  bindingError?: unknown;
  skillStatusIntent?: { assetId: string; checked: boolean } | null;
  skillStatusErrorAssetId?: string | null;
  skillStatusError?: unknown;
}) {
  return (
    <AssetList
      kind={kind}
      source={source}
      items={source === "system" ? data.system_items : data.project_items}
      selectedAssetId={selectedAssetId}
      onSelect={onSelect}
      onToggleSystemBinding={onToggleSystemBinding}
      onToggleProjectSkillStatus={onToggleProjectSkillStatus}
      bindingIntent={bindingIntent}
      bindingErrorAssetId={bindingErrorAssetId}
      bindingError={bindingError}
      skillStatusIntent={skillStatusIntent}
      skillStatusErrorAssetId={skillStatusErrorAssetId}
      skillStatusError={skillStatusError}
    />
  );
}

function ProjectAssetCatalog({
  accountId,
  project,
  kind,
  layout,
  createOpen,
  initialSelectedAssetId,
  onCreateOpenChange,
  renderList,
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
  onCreateOpenChange: (open: boolean) => void;
  renderList?: (context: ProjectAssetListRenderContext) => ReactNode;
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
  const query = useProjectAssets(accountId, project.id, kind);
  const createAsset = useCreateProjectAsset(accountId, project.id, kind);
  const createVersion = useCreateProjectAssetVersion(
    accountId,
    project.id,
    kind === "agents" ? null : kind,
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
  const [skillStatusIntent, setSkillStatusIntent] = useState<{
    assetId: string;
    checked: boolean;
  } | null>(null);
  const [versionAsset, setVersionAsset] = useState<ProjectAssetItem | null>(
    null,
  );
  const versionDialogGeneration = useRef(0);
  const activeVersionDialog = useRef<VersionDialogSubmissionToken | null>(null);
  const [createdVersions, setCreatedVersions] = useState<
    Record<string, string>
  >({});
  const [importedSkillSelection, setImportedSkillSelection] =
    useState<ImportedSkillSelection | null>(null);
  const [createdAssetId, setCreatedAssetId] = useState<string | null>(null);
  const [versionSubmission, setVersionSubmission] = useState<{
    token: VersionDialogSubmissionToken;
    pending: boolean;
    error: unknown;
  } | null>(null);

  useEffect(() => {
    if (!createAsset.isSuccess) return;
    onCreateOpenChange(false);
    if ((kind !== "skills" && kind !== "agents") || !createAsset.data) return;
    setSourceTouched(true);
    setSourceFilter("project");
    setCreatedAssetId(createAsset.data.item.id);
  }, [createAsset.data, createAsset.isSuccess, kind, onCreateOpenChange]);

  const handleRequestedVersionHandled = useCallback(
    (assetId: string, versionId: string) => {
      setCreatedVersions((current) =>
        handleRequestedVersion(current, assetId, versionId),
      );
    },
    [],
  );

  const rememberVersion = useCallback((assetId: string, versionId: string) => {
    setCreatedVersions((current) =>
      rememberRequestedVersion(current, assetId, versionId),
    );
  }, []);

  function openVersionDialog(
    item: ProjectAssetItem,
    _selectedVersion: AssetVersion | null,
  ) {
    if (kind === "agents") return;
    const token = {
      assetId: item.id,
      generation: ++versionDialogGeneration.current,
    };
    activeVersionDialog.current = token;
    createVersion.reset();
    setVersionSubmission(null);
    setVersionAsset(item);
  }

  function closeVersionDialog() {
    const token = activeVersionDialog.current;
    activeVersionDialog.current = null;
    createVersion.reset();
    setVersionAsset(null);
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

  const data = query.data as ProjectAssetList | undefined;
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

  useEffect(() => {
    const readySelection = importedSkillSelectionReady(
      data,
      importedSkillSelection,
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
    setImportedSkillSelection(null);
  }, [data, importedSkillSelection]);

  useEffect(() => {
    const readyAssetId = createdProjectAssetSelectionReady(
      data,
      createdAssetId,
    );
    if (!readyAssetId) return;
    setSelectedAssetId(readyAssetId);
    setCreatedAssetId(null);
  }, [createdAssetId, data]);

  useEffect(() => {
    if (selectedAssetId && data && !selectedItem) setSelectedAssetId(null);
  }, [data, selectedAssetId, selectedItem]);

  function toggleSystemBinding(item: ProjectAssetItem, checked: boolean) {
    if (kind === "mcp-servers") return;
    const state = systemBindingToggleState(item);
    if (state.disabled || state.checked === checked) return;

    enableBinding.reset();
    disableBinding.reset();
    setBindingIntent({ assetId: item.id, checked });
    const settle = () =>
      setBindingIntent((current) =>
        current?.assetId === item.id ? null : current,
      );

    if (checked && state.targetVersionId) {
      enableBinding.mutate(
        {
          asset_id: item.id,
          version_id: state.targetVersionId,
          ...(item.binding
            ? { expected_binding_version: item.binding.version }
            : {}),
        },
        { onSettled: settle },
      );
      return;
    }

    if (!checked && item.binding?.enabled) {
      disableBinding.mutate(
        {
          assetId: item.id,
          input: { expected_binding_version: item.binding.version },
        },
        { onSettled: settle },
      );
      return;
    }

    settle();
  }

  function toggleProjectSkillStatus(item: ProjectAssetItem, checked: boolean) {
    if (kind !== "skills") return;
    const state = projectSkillStatusToggleState(item);
    if (state.disabled || state.checked === checked) return;

    changeStatus.reset();
    setSkillStatusIntent({ assetId: item.id, checked });
    changeStatus.mutate(
      {
        assetId: item.id,
        action: checked ? "activate" : "suspend",
        input: { expected_asset_version: item.version },
      },
      {
        onSettled: () =>
          setSkillStatusIntent((current) =>
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
          {query.error
            ? adminAssetErrorMessage(query.error)
            : "资产列表暂时不可用。"}
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
  const bindingError = enableBinding.error ?? disableBinding.error;
  const bindingErrorAssetId = enableBinding.error
    ? enableBinding.variables?.asset_id
    : disableBinding.error
      ? disableBinding.variables?.assetId
      : null;
  const skillStatusErrorAssetId = changeStatus.error
    ? changeStatus.variables?.assetId
    : null;

  return (
    <>
      {renderLead?.({ project, data })}

      {layout === "agent-cards" && renderList ? (
        renderList({
          project,
          items: filteredData.project_items,
          source: "project",
          selectedAssetId,
          onSelect: (item) => setSelectedAssetId(item.id),
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
                        onBlank={() => onCreateOpenChange(true)}
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
                    onClick={() => onCreateOpenChange(true)}
                  >
                    <PlusIcon aria-hidden className="size-4" />
                    新建{KIND_META[kind].singular}
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
                    items,
                    source: value,
                    selectedAssetId,
                    onSelect: (item) => setSelectedAssetId(item.id),
                  })
                ) : (
                  <ProjectAssetListView
                    kind={kind}
                    data={filteredData}
                    source={value}
                    selectedAssetId={selectedAssetId}
                    onSelect={(item) => setSelectedAssetId(item.id)}
                    onToggleSystemBinding={toggleSystemBinding}
                    onToggleProjectSkillStatus={toggleProjectSkillStatus}
                    bindingIntent={bindingIntent}
                    bindingErrorAssetId={bindingErrorAssetId}
                    bindingError={bindingError}
                    skillStatusIntent={skillStatusIntent}
                    skillStatusErrorAssetId={skillStatusErrorAssetId}
                    skillStatusError={changeStatus.error}
                  />
                )}
              </TabsContent>
            );
          })}
        </Tabs>
      )}

      {selectedItem && (
        <ProjectAssetDetailSheet
          accountId={accountId}
          projectId={project.id}
          projectCapabilities={project.capabilities}
          kind={kind}
          item={selectedItem}
          open
          requestedVersionId={createdVersions[selectedItem.id] ?? null}
          onOpenChange={(next) => !next && setSelectedAssetId(null)}
          onCreateVersion={openVersionDialog}
          onDeleted={(assetId) => {
            setSelectedAssetId((current) =>
              current === assetId ? null : current,
            );
            setCreatedVersions((current) => {
              if (!(assetId in current)) return current;
              const next = { ...current };
              delete next[assetId];
              return next;
            });
          }}
          onVersionCreated={rememberVersion}
          onRequestedVersionHandled={handleRequestedVersionHandled}
          renderAssetEditor={renderAssetEditor}
          renderVersion={renderVersion}
        />
      )}

      {kind !== "agents" ? (
        <CreateAssetDialog
          kind={kind}
          scope="project"
          open={createOpen}
          pending={createAsset.isPending}
          errorMessage={
            createAsset.error
              ? projectAssetCreateErrorMessage(kind, createAsset.error)
              : null
          }
          onOpenChange={onCreateOpenChange}
          onSubmit={(input) => createAsset.mutate(input)}
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

      {versionAsset && kind !== "agents" ? (
        <CreateVersionDialog
          key={`${versionAsset.id}:${activeVersionDialog.current?.generation ?? "closed"}`}
          kind={kind}
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
              ? adminAssetErrorMessage(versionSubmission.error)
              : null
          }
          onOpenChange={(next) => !next && closeVersionDialog()}
          onSubmit={(input: VersionAuthoringInput) => void submitVersion(input)}
        />
      ) : null}
    </>
  );
}

export function ProjectAssetPageShell({
  kind,
  title,
  description,
  layout = "default",
  initialSelectedAssetId = null,
  renderList,
  renderAssetEditor,
  renderVersion,
  renderLead,
}: {
  kind: MutableAssetKind;
  title: string;
  description: string;
  layout?: ProjectAssetPageLayout;
  initialSelectedAssetId?: string | null;
  renderList?: (context: ProjectAssetListRenderContext) => ReactNode;
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
        eyebrow={`${project.display_name} · 项目资产`}
        title={title}
        description={description}
        actions={
          layout === "agent-cards" &&
          agentBuilderCanAuthor(project.capabilities) ? (
            <Button asChild>
              <Link
                href={`/projects/${encodeURIComponent(project.slug)}/agents/new`}
              >
                <PlusIcon aria-hidden className="size-4" />
                新建 Agent
              </Link>
            </Button>
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
        onCreateOpenChange={setCreateOpen}
        renderList={renderList}
        renderAssetEditor={renderAssetEditor}
        renderVersion={renderVersion}
        renderLead={renderLead}
      />
    </main>
  );
}
