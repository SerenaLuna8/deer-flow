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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Project } from "@/core/projects/types";
import {
  useCreateProjectAsset,
  useCreateProjectAssetVersion,
  useProjectAssets,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

import { useCurrentProject } from "../project-context";

import { ProjectAssetDetailSheet } from "./project-asset-detail-sheet";
import type { ProjectAssetVersionRenderContext } from "./project-asset-detail-sheet";

type MutableAssetKind = Exclude<AssetListKind, "credentials">;
export type ProjectAssetSourceFilter = "all" | "system" | "project";

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

export function filterProjectAssetList(
  data: ProjectAssetList,
  searchQuery: string,
  source: ProjectAssetSourceFilter,
): ProjectAssetList {
  const query = searchQuery.trim().toLocaleLowerCase();
  const matches = (item: ProjectAssetItem) =>
    query === "" ||
    item.display_name.toLocaleLowerCase().includes(query) ||
    item.slug.toLocaleLowerCase().includes(query);

  return {
    ...data,
    system_items: source === "project" ? [] : data.system_items.filter(matches),
    project_items:
      source === "system" ? [] : data.project_items.filter(matches),
  };
}

function AssetListSection({
  kind,
  title,
  description,
  items,
  selectedAssetId,
  onSelect,
}: {
  kind: MutableAssetKind;
  title: string;
  description: string;
  items: ProjectAssetItem[];
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
}) {
  const Icon = KIND_META[kind].icon;

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-base font-semibold">{title}</h2>
        <p className="text-muted-foreground mt-1 text-sm">{description}</p>
      </div>

      {items.length === 0 ? (
        <p className="text-muted-foreground rounded-2xl border border-dashed px-5 py-8 text-center text-sm">
          暂无{title}的 {KIND_META[kind].singular}。
        </p>
      ) : (
        <div className="overflow-hidden rounded-2xl border">
          {items.map((item) => {
            const selected = item.id === selectedAssetId;
            const isSystem = item.scope === "system";
            return (
              <button
                key={item.id}
                type="button"
                aria-haspopup="dialog"
                className={`hover:bg-muted/45 focus-visible:ring-ring group flex w-full items-center gap-4 border-b px-4 py-4 text-left transition-colors last:border-b-0 focus-visible:ring-2 focus-visible:outline-none sm:px-5 ${
                  selected ? "bg-muted/60" : ""
                }`}
                onClick={() => onSelect(item)}
              >
                <span className="bg-muted flex size-10 shrink-0 items-center justify-center rounded-xl">
                  <Icon aria-hidden className="size-5" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-semibold">
                      {item.display_name}
                    </span>
                    <Badge variant={isSystem ? "secondary" : "default"}>
                      {isSystem ? "系统提供" : "项目自建"}
                    </Badge>
                    <AssetStatusBadge status={item.status} />
                  </span>
                  <span className="text-muted-foreground mt-1 block truncate font-mono text-xs">
                    {item.slug}
                  </span>
                  <span className="mt-2 block text-xs font-medium sm:hidden">
                    {assetAvailability(item)}
                  </span>
                </span>
                <span className="hidden shrink-0 text-right sm:block">
                  <span className="text-sm font-medium">
                    {assetAvailability(item)}
                  </span>
                  <time className="text-muted-foreground mt-1 block text-xs">
                    {new Date(item.updated_at).toLocaleDateString("zh-CN")}
                  </time>
                </span>
                <ArrowRightIcon
                  aria-hidden
                  className="text-muted-foreground size-4 shrink-0 transition-transform group-hover:translate-x-0.5"
                />
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}

export function ProjectAssetListView({
  kind,
  data,
  selectedAssetId,
  onSelect,
}: {
  kind: MutableAssetKind;
  data: ProjectAssetList;
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
}) {
  return (
    <div className="space-y-9">
      <AssetListSection
        kind={kind}
        title="系统提供"
        description="平台维护的只读资产；启用后项目固定使用指定发布版本。"
        items={data.system_items}
        selectedAssetId={selectedAssetId}
        onSelect={onSelect}
      />
      <AssetListSection
        kind={kind}
        title="项目自建"
        description="仅属于当前项目，可创建新版本并按发布流程生效。"
        items={data.project_items}
        selectedAssetId={selectedAssetId}
        onSelect={onSelect}
      />
    </div>
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
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [sourceFilter, setSourceFilter] =
    useState<ProjectAssetSourceFilter>("all");
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
  const filteredData = useMemo(
    () =>
      data
        ? filterProjectAssetList(data, searchQuery, sourceFilter)
        : undefined,
    [data, searchQuery, sourceFilter],
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

  if (query.isLoading) {
    return (
      <div className="space-y-8" aria-label="正在加载资产">
        <Skeleton className="h-24 w-full rounded-2xl" />
        <Skeleton className="h-48 w-full rounded-2xl" />
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
  const totalCount = data.system_items.length + data.project_items.length;
  const visibleCount =
    filteredData.system_items.length + filteredData.project_items.length;
  const filterActive = searchQuery.trim() !== "" || sourceFilter !== "all";
  const sourceOptions = [
    ["all", "全部"],
    ["system", "系统提供"],
    ["project", "项目自建"],
  ] as const;

  return (
    <>
      {renderLead?.({ project, data })}

      <div className="mb-6 flex items-center justify-between gap-4">
        <p className="text-muted-foreground text-sm">
          {filterActive
            ? `显示 ${visibleCount} / 共 ${totalCount} 项`
            : `共 ${totalCount} 项`}
        </p>
        {canCreate && (
          <Button type="button" onClick={() => setCreateOpen(true)}>
            <PlusIcon aria-hidden className="size-4" />
            新建{KIND_META[kind].singular}
          </Button>
        )}
      </div>

      <div className="bg-muted/20 mb-8 flex flex-col gap-3 rounded-2xl border p-3 sm:flex-row sm:items-center">
        <label className="relative min-w-0 flex-1">
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
            className="bg-background pl-9"
          />
        </label>
        <div
          role="group"
          aria-label="来源筛选"
          className="bg-background flex w-full gap-1 rounded-xl border p-1 sm:w-auto"
        >
          {sourceOptions.map(([value, label]) => (
            <Button
              key={value}
              type="button"
              size="sm"
              variant={sourceFilter === value ? "default" : "ghost"}
              className="flex-1 sm:flex-none"
              aria-pressed={sourceFilter === value}
              onClick={() => setSourceFilter(value)}
            >
              {label}
            </Button>
          ))}
        </div>
      </div>

      {filterActive && visibleCount === 0 && (
        <p
          role="status"
          className="text-muted-foreground mb-6 rounded-2xl border border-dashed px-5 py-6 text-center text-sm"
        >
          没有找到匹配的 {KIND_META[kind].singular}，请调整搜索词或来源筛选。
        </p>
      )}

      <ProjectAssetListView
        kind={kind}
        data={filteredData}
        selectedAssetId={selectedAssetId}
        onSelect={(item) => setSelectedAssetId(item.id)}
      />

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
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
      <header className="mb-8 max-w-3xl">
        <p className="text-primary mb-2 text-sm font-medium">
          {project.display_name} · 项目资产
        </p>
        <h1 className="text-3xl font-semibold tracking-tight">{title}</h1>
        <p className="text-muted-foreground mt-2 leading-6">{description}</p>
      </header>

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
