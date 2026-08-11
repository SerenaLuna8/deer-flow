"use client";

import { ArchiveIcon, PlusIcon, SearchIcon, WorkflowIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { WorkflowDefinitionResponseV1 } from "@/core/project-workflows/definition-contracts";

export type WorkflowDefinitionListFilters = {
  query: string;
  lifecycle: "active" | "archived";
  publication: "all" | "draft_only" | "published";
  sort: "updated_desc" | "name_asc" | "name_desc";
};

export const defaultWorkflowDefinitionListFilters: WorkflowDefinitionListFilters =
  {
    query: "",
    lifecycle: "active",
    publication: "all",
    sort: "updated_desc",
  };

export type WorkflowDefinitionListState =
  | { status: "loading" }
  | { status: "filtering" }
  | { status: "disabled" }
  | { status: "error"; retry: () => void }
  | {
      status: "ready";
      items: readonly WorkflowDefinitionResponseV1[];
      nextCursor: string | null;
      loadingMore: boolean;
    };

export type WorkflowDefinitionListShellProps = {
  state: WorkflowDefinitionListState;
  filters: WorkflowDefinitionListFilters;
  canEdit: boolean;
  onFiltersChange: (filters: WorkflowDefinitionListFilters) => void;
  onCreateBlank: () => void;
  onOpen: (definition: WorkflowDefinitionResponseV1) => void;
  onArchive: (definition: WorkflowDefinitionResponseV1) => void;
  onLoadMore: () => void;
};

function hasActiveFilters(filters: WorkflowDefinitionListFilters): boolean {
  return (
    filters.query.trim().length > 0 ||
    filters.lifecycle !== "active" ||
    filters.publication !== "all"
  );
}

function updatedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "更新时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function Filters({
  filters,
  onChange,
}: {
  filters: WorkflowDefinitionListFilters;
  onChange: (filters: WorkflowDefinitionListFilters) => void;
}) {
  return (
    <div className="grid gap-3 rounded-xl border p-3 md:grid-cols-[minmax(14rem,1fr)_auto_auto_auto]">
      <label className="relative min-w-0">
        <span className="sr-only">搜索工作流</span>
        <SearchIcon
          aria-hidden
          className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
        />
        <Input
          type="search"
          aria-label="搜索工作流"
          placeholder="搜索名称或描述"
          maxLength={255}
          value={filters.query}
          className="pl-9"
          onChange={(event) =>
            onChange({ ...filters, query: event.currentTarget.value })
          }
        />
      </label>
      <label className="grid gap-1 text-xs">
        <span className="text-muted-foreground">生命周期</span>
        <select
          aria-label="生命周期"
          value={filters.lifecycle}
          className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          onChange={(event) =>
            onChange({
              ...filters,
              lifecycle: event.currentTarget.value as "active" | "archived",
            })
          }
        >
          <option value="active">使用中</option>
          <option value="archived">已归档</option>
        </select>
      </label>
      <label className="grid gap-1 text-xs">
        <span className="text-muted-foreground">发布状态</span>
        <select
          aria-label="发布状态"
          value={filters.publication}
          className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          onChange={(event) =>
            onChange({
              ...filters,
              publication: event.currentTarget.value as
                | "all"
                | "draft_only"
                | "published",
            })
          }
        >
          <option value="all">全部</option>
          <option value="draft_only">仅草稿</option>
          <option value="published">已发布</option>
        </select>
      </label>
      <label className="grid gap-1 text-xs">
        <span className="text-muted-foreground">排序</span>
        <select
          aria-label="排序"
          value={filters.sort}
          className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          onChange={(event) =>
            onChange({
              ...filters,
              sort: event.currentTarget.value as
                | "updated_desc"
                | "name_asc"
                | "name_desc",
            })
          }
        >
          <option value="updated_desc">最近更新</option>
          <option value="name_asc">名称 A–Z</option>
          <option value="name_desc">名称 Z–A</option>
        </select>
      </label>
    </div>
  );
}

function EmptyState({
  canEdit,
  filtered,
  onCreateBlank,
  onClearFilters,
}: {
  canEdit: boolean;
  filtered: boolean;
  onCreateBlank: () => void;
  onClearFilters: () => void;
}) {
  if (filtered) {
    return (
      <section
        data-testid="workflow-filter-empty"
        className="rounded-2xl border border-dashed px-6 py-16 text-center"
      >
        <h2 className="font-medium">没有符合筛选条件的工作流</h2>
        <p className="text-muted-foreground mt-2 text-sm">
          调整搜索、生命周期或发布状态后重试。
        </p>
        <Button
          type="button"
          variant="outline"
          className="mt-5"
          onClick={onClearFilters}
        >
          清除筛选
        </Button>
      </section>
    );
  }

  return (
    <section
      data-testid="workflow-empty"
      className="rounded-2xl border border-dashed px-6 py-16 text-center"
    >
      <WorkflowIcon
        aria-hidden
        className="text-muted-foreground mx-auto size-9"
      />
      <h2 className="mt-4 font-medium">
        {canEdit ? "还没有工作流" : "当前没有可查看的工作流"}
      </h2>
      <p className="text-muted-foreground mx-auto mt-2 max-w-md text-sm">
        {canEdit
          ? "从一个空白工作流开始，在 Builder 中添加和连接节点。"
          : "具有编辑权限的项目成员创建后，工作流会显示在这里。"}
      </p>
      {canEdit && (
        <Button type="button" className="mt-5" onClick={onCreateBlank}>
          <PlusIcon aria-hidden className="size-4" />
          创建空白工作流
        </Button>
      )}
    </section>
  );
}

export function WorkflowDefinitionListShell({
  state,
  filters,
  canEdit,
  onFiltersChange,
  onCreateBlank,
  onOpen,
  onArchive,
  onLoadMore,
}: WorkflowDefinitionListShellProps) {
  if (state.status === "loading") {
    return (
      <main
        aria-busy="true"
        aria-label="正在加载工作流"
        className="mx-auto w-full max-w-7xl space-y-4 px-4 py-8 sm:px-6 lg:px-8"
      >
        <div className="bg-muted h-9 w-44 animate-pulse rounded-md" />
        <div className="bg-muted h-24 animate-pulse rounded-xl" />
        <div className="bg-muted h-48 animate-pulse rounded-xl" />
      </main>
    );
  }

  if (state.status === "disabled") {
    return (
      <main className="mx-auto w-full max-w-3xl px-4 py-16 sm:px-6">
        <section
          data-testid="workflow-disabled"
          className="rounded-2xl border px-6 py-12 text-center"
        >
          <h1 className="text-xl font-semibold">平台未启用工作流</h1>
          <p className="text-muted-foreground mt-3 text-sm">
            平台管理员已确认关闭工作流控制面。已有项目数据不会被显示为空列表。
          </p>
        </section>
      </main>
    );
  }

  if (state.status === "error") {
    return (
      <main className="mx-auto w-full max-w-3xl px-4 py-16 sm:px-6">
        <section
          data-testid="workflow-unavailable"
          role="alert"
          className="rounded-2xl border px-6 py-12 text-center"
        >
          <h1 className="text-xl font-semibold">暂时无法确认工作流状态</h1>
          <p className="text-muted-foreground mt-3 text-sm">
            控制面暂时不可用。请重试；此状态不会被当作空列表。
          </p>
          <Button
            type="button"
            variant="outline"
            className="mt-5"
            onClick={state.retry}
          >
            重试
          </Button>
        </section>
      </main>
    );
  }

  const filtered = hasActiveFilters(filters);
  return (
    <main className="mx-auto w-full max-w-7xl space-y-6 px-4 py-8 sm:px-6 lg:px-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">工作流</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            查看项目共享的 Definition，并进入独立 Builder。
          </p>
        </div>
        {canEdit && (
          <Button type="button" onClick={onCreateBlank}>
            <PlusIcon aria-hidden className="size-4" />
            创建空白工作流
          </Button>
        )}
      </header>

      <Filters filters={filters} onChange={onFiltersChange} />

      {state.status === "filtering" ? (
        <section
          aria-busy="true"
          aria-label="正在筛选工作流"
          data-testid="workflow-filtering"
          className="space-y-3"
        >
          <div className="bg-muted h-24 animate-pulse rounded-xl" />
          <div className="bg-muted h-24 animate-pulse rounded-xl" />
        </section>
      ) : state.items.length === 0 ? (
        <EmptyState
          canEdit={canEdit}
          filtered={filtered}
          onCreateBlank={onCreateBlank}
          onClearFilters={() =>
            onFiltersChange(defaultWorkflowDefinitionListFilters)
          }
        />
      ) : (
        <>
          <section aria-label="工作流列表" className="grid gap-3">
            {state.items.map((definition) => (
              <article
                key={definition.id}
                className="flex flex-col gap-4 rounded-xl border p-4 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="truncate font-medium">{definition.name}</h2>
                    <Badge variant="outline">
                      {definition.publication === "published"
                        ? "已发布"
                        : "仅草稿"}
                    </Badge>
                    {definition.current_published_version_number !== null && (
                      <Badge variant="secondary">
                        版本 {definition.current_published_version_number}
                      </Badge>
                    )}
                  </div>
                  {definition.description && (
                    <p className="text-muted-foreground mt-1 line-clamp-2 text-sm">
                      {definition.description}
                    </p>
                  )}
                  <p className="text-muted-foreground mt-2 text-xs">
                    更新于 {updatedAt(definition.updated_at)}
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    aria-label={`${canEdit && definition.lifecycle === "active" ? "编辑" : "查看"} ${definition.name}`}
                    onClick={() => onOpen(definition)}
                  >
                    {canEdit && definition.lifecycle === "active"
                      ? "编辑"
                      : "查看"}
                  </Button>
                  {canEdit && definition.lifecycle === "active" && (
                    <Button
                      type="button"
                      variant="ghost"
                      aria-label={`归档 ${definition.name}`}
                      onClick={() => onArchive(definition)}
                    >
                      <ArchiveIcon aria-hidden className="size-4" />
                      归档
                    </Button>
                  )}
                </div>
              </article>
            ))}
          </section>
          {state.nextCursor !== null && (
            <div className="flex justify-center">
              <Button
                type="button"
                variant="outline"
                disabled={state.loadingMore}
                onClick={onLoadMore}
              >
                {state.loadingMore ? "正在加载…" : "加载更多"}
              </Button>
            </div>
          )}
        </>
      )}
    </main>
  );
}
