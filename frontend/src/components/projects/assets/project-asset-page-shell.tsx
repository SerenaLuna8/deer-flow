"use client";

import {
  ArrowRightIcon,
  BotIcon,
  PlugZapIcon,
  PlusIcon,
  SearchIcon,
  SparklesIcon,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import {
  CreateAssetDialog,
  CreateVersionDialog,
  type VersionAuthoringInput,
} from "@/components/admin/assets/admin-asset-dialogs";
import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/core/auth/AuthProvider";
import { useModels } from "@/core/models/hooks";
import type { Project } from "@/core/projects/types";
import {
  useCreateProjectAsset,
  useCreateProjectAssetVersion,
  useDisableProjectSystemBinding,
  useEnableProjectSystemBinding,
  useProjectAssets,
  type AssetKind,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

import { useCurrentProject } from "../project-context";
import { ProjectPageHeader } from "../project-page-header";

import { dependencyVersionOptions } from "./asset-dependency-options";
import { ProjectAssetDetailSheet } from "./project-asset-detail-sheet";
import type { ProjectAssetVersionRenderContext } from "./project-asset-detail-sheet";

type MutableAssetKind = Exclude<AssetListKind, "credentials">;
export type ProjectAssetSourceFilter = "system" | "project";

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

function AssetList({
  kind,
  source,
  items,
  selectedAssetId,
  onSelect,
  onToggleSystemBinding,
  bindingIntent,
  bindingErrorAssetId,
  bindingError,
}: {
  kind: MutableAssetKind;
  source: ProjectAssetSourceFilter;
  items: ProjectAssetItem[];
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
  onToggleSystemBinding: (item: ProjectAssetItem, checked: boolean) => void;
  bindingIntent?: { assetId: string; checked: boolean } | null;
  bindingErrorAssetId?: string | null;
  bindingError?: unknown;
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
        const error =
          bindingErrorAssetId === item.id && bindingError
            ? adminAssetErrorMessage(bindingError)
            : null;
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
                    {item.status !== "active" ? (
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
                {kind !== "skills" ? (
                  <span className="text-muted-foreground hidden text-xs sm:inline">
                    {checked ? "已启用" : "未启用"}
                  </span>
                ) : null}
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
                  onCheckedChange={(next) => onToggleSystemBinding(item, next)}
                />
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
  bindingIntent,
  bindingErrorAssetId,
  bindingError,
}: {
  kind: MutableAssetKind;
  data: ProjectAssetList;
  source: ProjectAssetSourceFilter;
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
  onToggleSystemBinding: (item: ProjectAssetItem, checked: boolean) => void;
  bindingIntent?: { assetId: string; checked: boolean } | null;
  bindingErrorAssetId?: string | null;
  bindingError?: unknown;
}) {
  return (
    <AssetList
      kind={kind}
      source={source}
      items={source === "system" ? data.system_items : data.project_items}
      selectedAssetId={selectedAssetId}
      onSelect={onSelect}
      onToggleSystemBinding={onToggleSystemBinding}
      bindingIntent={bindingIntent}
      bindingErrorAssetId={bindingErrorAssetId}
      bindingError={bindingError}
    />
  );
}

function ProjectAssetCatalog({
  accountId,
  project,
  kind,
  renderVersion,
  renderLead,
}: {
  accountId: string;
  project: Project;
  kind: MutableAssetKind;
  renderVersion: (
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
    kind,
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
  const modelCatalog = useModels({ enabled: kind === "agents" });
  const skillDependencies = useProjectAssets(
    accountId,
    project.id,
    "skills",
    kind === "agents",
  );
  const mcpDependencies = useProjectAssets(
    accountId,
    project.id,
    "mcp-servers",
    kind === "agents",
  );
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [sourceFilter, setSourceFilter] =
    useState<ProjectAssetSourceFilter>("project");
  const [sourceTouched, setSourceTouched] = useState(false);
  const [bindingIntent, setBindingIntent] = useState<{
    assetId: string;
    checked: boolean;
  } | null>(null);
  const [versionAsset, setVersionAsset] = useState<ProjectAssetItem | null>(
    null,
  );

  useEffect(() => {
    if (createAsset.isSuccess) setCreateOpen(false);
  }, [createAsset.isSuccess]);

  useEffect(() => {
    if (createVersion.isSuccess) setVersionAsset(null);
  }, [createVersion.isSuccess]);

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
    if (selectedAssetId && data && !selectedItem) setSelectedAssetId(null);
  }, [data, selectedAssetId, selectedItem]);

  function toggleSystemBinding(item: ProjectAssetItem, checked: boolean) {
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

  return (
    <>
      {renderLead?.({ project, data })}

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
            <Button
              type="button"
              size="sm"
              className="shrink-0"
              onClick={() => setCreateOpen(true)}
            >
              <PlusIcon aria-hidden className="size-4" />
              新建{KIND_META[kind].singular}
            </Button>
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
              ) : (
                <ProjectAssetListView
                  kind={kind}
                  data={filteredData}
                  source={value}
                  selectedAssetId={selectedAssetId}
                  onSelect={(item) => setSelectedAssetId(item.id)}
                  onToggleSystemBinding={toggleSystemBinding}
                  bindingIntent={bindingIntent}
                  bindingErrorAssetId={bindingErrorAssetId}
                  bindingError={bindingError}
                />
              )}
            </TabsContent>
          );
        })}
      </Tabs>

      {selectedItem && (
        <ProjectAssetDetailSheet
          accountId={accountId}
          projectId={project.id}
          projectCapabilities={project.capabilities}
          kind={kind}
          item={selectedItem}
          open
          onOpenChange={(next) => !next && setSelectedAssetId(null)}
          onCreateVersion={setVersionAsset}
          renderVersion={renderVersion}
        />
      )}

      <CreateAssetDialog
        kind={kind}
        scope="project"
        open={createOpen}
        pending={createAsset.isPending}
        errorMessage={
          createAsset.error ? adminAssetErrorMessage(createAsset.error) : null
        }
        onOpenChange={setCreateOpen}
        onSubmit={(input) => createAsset.mutate(input)}
      />

      {versionAsset && (
        <CreateVersionDialog
          kind={kind}
          asset={versionAsset}
          open
          pending={createVersion.isPending}
          errorMessage={
            createVersion.error
              ? adminAssetErrorMessage(createVersion.error)
              : null
          }
          modelOptions={modelCatalog.models.map((model) => ({
            name: model.name,
            displayName: model.display_name,
          }))}
          skillVersionOptions={dependencyVersionOptions(
            skillDependencies.data as ProjectAssetList | undefined,
          )}
          mcpVersionOptions={dependencyVersionOptions(
            mcpDependencies.data as ProjectAssetList | undefined,
          )}
          modelsLoading={modelCatalog.isLoading}
          modelsError={Boolean(modelCatalog.error)}
          onOpenChange={(next) => !next && setVersionAsset(null)}
          onSubmit={(input: VersionAuthoringInput) =>
            createVersion.mutate({
              assetId: versionAsset.id,
              input,
            })
          }
        />
      )}
    </>
  );
}

export function ProjectAssetPageShell({
  kind,
  title,
  description,
  renderVersion,
  renderLead,
}: {
  kind: MutableAssetKind;
  title: string;
  description: string;
  renderVersion: (
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

  if (!user) return null;

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
      <ProjectPageHeader
        className="mb-5"
        eyebrow={`${project.display_name} · 项目资产`}
        title={title}
        description={description}
      />

      <ProjectAssetCatalog
        accountId={user.id}
        project={project}
        kind={kind}
        renderVersion={renderVersion}
        renderLead={renderLead}
      />
    </main>
  );
}
