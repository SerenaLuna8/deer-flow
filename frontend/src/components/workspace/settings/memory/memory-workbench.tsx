"use client";

import {
  AlertCircleIcon,
  BrainIcon,
  BriefcaseBusinessIcon,
  ChevronDownIcon,
  Clock3Icon,
  DatabaseIcon,
  DownloadIcon,
  FileTextIcon,
  FolderGit2Icon,
  HeartIcon,
  Layers3Icon,
  MoreHorizontalIcon,
  NotebookTextIcon,
  PenLineIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
  SparklesIcon,
  StarIcon,
  Trash2Icon,
  UploadIcon,
} from "lucide-react";
import Link from "next/link";
import type * as React from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  confidenceToLevelKey,
  getMemoryCategoryVisual,
  getMemorySummaryPreviews,
  type MemoryFact,
  type MemorySectionGroup,
  type MemoryViewFilter,
  upperFirst,
} from "@/components/workspace/settings/memory/memory-view-model";
import type { Translations } from "@/core/i18n/locales/types";
import { SafeStreamdown } from "@/core/streamdown/components";
import { streamdownPlugins } from "@/core/streamdown/plugins";
import { formatTimeAgo } from "@/core/utils/datetime";
import { cn } from "@/lib/utils";

export type MemorySourceThreadHref = (fact: MemoryFact) => string;

export function MemoryHeaderActions(props: {
  t: Translations;
  isImporting: boolean;
  isExporting: boolean;
  isClearing: boolean;
  isReloading?: boolean;
  canAddFact?: boolean;
  canImport?: boolean;
  canExport?: boolean;
  canClear?: boolean;
  canReload?: boolean;
  onAddFact: () => void;
  onImport: () => void;
  onExport: () => void;
  onClear: () => void;
  onReload?: () => void;
}): React.ReactNode {
  const {
    t,
    isImporting,
    isExporting,
    isClearing,
    isReloading = false,
    canAddFact = true,
    canImport = true,
    canExport = true,
    canClear = true,
    canReload = false,
    onAddFact,
    onImport,
    onExport,
    onClear,
    onReload,
  } = props;

  const hasManageActions = canImport || canExport || canClear || canReload;

  return (
    <div className="flex flex-wrap items-center gap-2">
      {hasManageActions ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline">
              <DatabaseIcon aria-hidden="true" />
              {t.settings.memory.manageMemory}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-52">
            {canReload ? (
              <DropdownMenuItem disabled={isReloading} onSelect={onReload}>
                <RefreshCwIcon aria-hidden="true" />
                {isReloading ? t.common.loading : t.common.reload}
              </DropdownMenuItem>
            ) : null}
            {canImport ? (
              <DropdownMenuItem disabled={isImporting} onSelect={onImport}>
                <UploadIcon aria-hidden="true" />
                {t.settings.memory.importButton}
              </DropdownMenuItem>
            ) : null}
            {canExport ? (
              <DropdownMenuItem disabled={isExporting} onSelect={onExport}>
                <DownloadIcon aria-hidden="true" />
                {isExporting
                  ? t.common.loading
                  : t.settings.memory.exportButton}
              </DropdownMenuItem>
            ) : null}
            {canClear ? <DropdownMenuSeparator /> : null}
            {canClear ? (
              <DropdownMenuItem
                variant="destructive"
                disabled={isClearing}
                onSelect={onClear}
              >
                <Trash2Icon aria-hidden="true" />
                {isClearing ? t.common.loading : t.settings.memory.clearAll}
              </DropdownMenuItem>
            ) : null}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
      {canAddFact ? (
        <Button onClick={onAddFact}>
          <PlusIcon aria-hidden="true" />
          {t.settings.memory.addFact}
        </Button>
      ) : null}
    </div>
  );
}

export function MemoryStatusBar(props: {
  t: Translations;
  factCount: number;
  summaryCount: number;
  lastUpdated: string;
}): React.ReactNode {
  const { t, factCount, summaryCount, lastUpdated } = props;

  return (
    <section
      data-testid="memory-status-bar"
      aria-label={t.settings.memory.markdown.overview}
      className="text-muted-foreground flex flex-wrap items-center gap-x-5 gap-y-2 text-sm"
    >
      <dl className="flex flex-wrap items-center gap-x-5 gap-y-2">
        <div className="flex items-center gap-2">
          <FileTextIcon aria-hidden="true" className="size-4" />
          <dt className="sr-only">{t.settings.memory.markdown.facts}</dt>
          <dd className="text-foreground font-medium tabular-nums">
            {t.settings.memory.factCount(factCount)}
          </dd>
        </div>

        <div className="border-border flex items-center gap-2 border-l pl-5">
          <Layers3Icon aria-hidden="true" className="size-4" />
          <dt className="sr-only">{t.settings.memory.smartSummaries}</dt>
          <dd className="text-foreground font-medium tabular-nums">
            {t.settings.memory.summaryCount(summaryCount)}
          </dd>
        </div>

        <div className="border-border flex min-w-0 items-center gap-2 border-l pl-5">
          <Clock3Icon aria-hidden="true" className="size-4" />
          <dt>{t.common.lastUpdated}</dt>
          <dd className="text-foreground truncate font-medium">
            {lastUpdated}
          </dd>
        </div>
      </dl>
    </section>
  );
}

export function MemoryContextSidecar(props: {
  t: Translations;
  recentFocus: string;
  groups: MemorySectionGroup[];
  summaryCount: number;
  onViewSummaries: () => void;
}): React.ReactNode {
  const { t, recentFocus, groups, summaryCount, onViewSummaries } = props;
  const previews = getMemorySummaryPreviews(
    groups,
    t.settings.memory.markdown.topOfMind,
  );

  return (
    <aside
      data-testid="memory-context-sidecar"
      aria-labelledby="memory-context-sidecar-heading"
      className="bg-card min-w-0 rounded-xl border p-5 lg:p-6"
    >
      <section className="min-w-0">
        <div className="flex items-center gap-3">
          <StarIcon
            aria-hidden="true"
            className="text-muted-foreground size-5"
          />
          <h2
            id="memory-context-sidecar-heading"
            className="text-base font-semibold"
          >
            {t.settings.memory.recentFocus}
          </h2>
        </div>
        <SafeStreamdown
          className="text-foreground/90 mt-5 min-w-0 text-sm leading-7 [overflow-wrap:anywhere] [&>*:first-child]:mt-0 [&>*:last-child]:mb-0"
          {...streamdownPlugins}
        >
          {recentFocus}
        </SafeStreamdown>
        <Button
          type="button"
          variant="link"
          size="sm"
          className="text-primary mt-3 h-auto px-0 py-0 text-sm"
          onClick={onViewSummaries}
        >
          {t.settings.memory.viewSummaries}
        </Button>
      </section>

      <section
        aria-labelledby="memory-sidecar-summaries-heading"
        className="border-border mt-6 min-w-0 border-t pt-6"
      >
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <SparklesIcon
              aria-hidden="true"
              className="text-muted-foreground size-5 shrink-0"
            />
            <div className="min-w-0">
              <h2
                id="memory-sidecar-summaries-heading"
                className="text-base font-semibold"
              >
                {t.settings.memory.smartSummaries}
              </h2>
              <p className="text-muted-foreground mt-1 text-xs tabular-nums">
                {t.settings.memory.summaryCount(summaryCount)}
              </p>
            </div>
          </div>
          <span className="bg-muted text-muted-foreground rounded-md px-2 py-1 text-xs">
            {t.humanInput.readOnly}
          </span>
        </div>

        <div className="mt-6 space-y-6">
          {previews.length > 0 ? (
            previews.map((section) => (
              <article key={`${section.groupTitle}:${section.title}`}>
                <h3 className="text-sm font-semibold">{section.title}</h3>
                <SafeStreamdown
                  className="text-foreground/85 mt-2 min-w-0 text-sm leading-6 [overflow-wrap:anywhere] [&>*:first-child]:mt-0 [&>*:last-child]:mb-0"
                  {...streamdownPlugins}
                >
                  {section.summary}
                </SafeStreamdown>
                {section.updatedAt ? (
                  <p className="text-muted-foreground mt-2 text-xs">
                    {formatTimeAgo(section.updatedAt)}
                  </p>
                ) : null}
              </article>
            ))
          ) : (
            <p className="text-muted-foreground text-sm">
              {t.settings.memory.markdown.empty}
            </p>
          )}
        </div>
      </section>
    </aside>
  );
}

export function MemoryToolbar(props: {
  t: Translations;
  query: string;
  filter: MemoryViewFilter;
  factCount?: number;
  summaryCount?: number;
  embedded?: boolean;
  onQueryChange: (query: string) => void;
  onFilterChange: (filter: MemoryViewFilter) => void;
}): React.ReactNode {
  const {
    t,
    query,
    filter,
    factCount,
    summaryCount,
    embedded = false,
    onQueryChange,
    onFilterChange,
  } = props;
  const toggleItemClassName = cn(
    "whitespace-nowrap",
    embedded &&
      "data-[state=on]:border-selection h-10 rounded-none border-x-0 border-t-0 border-b-2 border-transparent bg-transparent px-3 shadow-none data-[state=on]:bg-transparent data-[state=on]:text-foreground",
  );

  return (
    <div
      className={cn(
        "flex flex-col gap-3 sm:flex-row sm:items-center",
        embedded
          ? "border-border border-b px-4 py-4 sm:px-5"
          : "bg-muted/25 rounded-xl border p-3",
      )}
    >
      <ToggleGroup
        type="single"
        value={filter}
        onValueChange={(value) => {
          if (value) onFilterChange(value as MemoryViewFilter);
        }}
        variant={embedded ? "default" : "outline"}
        aria-label={t.settings.memory.title}
        className={cn("max-w-full shrink-0 self-start", embedded && "gap-1")}
      >
        <ToggleGroupItem
          data-testid="memory-filter-all"
          value="all"
          className={toggleItemClassName}
        >
          {t.settings.memory.filterAll}
        </ToggleGroupItem>
        <ToggleGroupItem
          data-testid="memory-filter-facts"
          value="facts"
          className={toggleItemClassName}
        >
          {t.settings.memory.filterFacts}
          {typeof factCount === "number" ? (
            <span className="text-muted-foreground tabular-nums">
              {factCount}
            </span>
          ) : null}
        </ToggleGroupItem>
        <ToggleGroupItem
          data-testid="memory-filter-summaries"
          value="summaries"
          className={toggleItemClassName}
        >
          {t.settings.memory.filterSummaries}
          {typeof summaryCount === "number" ? (
            <span className="text-muted-foreground tabular-nums">
              {summaryCount}
            </span>
          ) : null}
        </ToggleGroupItem>
      </ToggleGroup>

      <div className="relative min-w-0 flex-1 sm:ml-auto sm:max-w-sm">
        <SearchIcon
          aria-hidden="true"
          className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
        />
        <Input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder={t.settings.memory.searchPlaceholder}
          aria-label={t.settings.memory.searchPlaceholder}
          className="pl-9"
        />
      </div>
    </div>
  );
}

function MemoryFactRow(props: {
  fact: MemoryFact;
  t: Translations;
  onEdit?: () => void;
  onDelete?: () => void;
  disabled: boolean;
  sourceThreadHref: MemorySourceThreadHref;
}): React.ReactNode {
  const { fact, t, onEdit, onDelete, disabled, sourceThreadHref } = props;
  const { key } = confidenceToLevelKey(fact.confidence);
  const confidenceText = t.settings.memory.markdown.table.confidenceLevel[key];
  const visual = getMemoryCategoryVisual(fact.category);
  const CategoryIcon = {
    preference: HeartIcon,
    work: BriefcaseBusinessIcon,
    project: FolderGit2Icon,
    context: NotebookTextIcon,
    default: BrainIcon,
  }[visual];

  return (
    <article
      data-testid={`memory-fact-row-${fact.id}`}
      className="hover:bg-muted/25 flex min-w-0 items-start gap-4 px-4 py-5 [overflow-wrap:anywhere] transition-colors sm:px-5 lg:py-11"
    >
      <div className="bg-muted/35 text-muted-foreground mt-0.5 flex size-10 shrink-0 items-center justify-center rounded-lg border">
        <CategoryIcon aria-hidden="true" className="size-[18px]" />
      </div>
      <div className="max-w-3xl min-w-0 flex-1 space-y-2">
        <p className="text-sm leading-6 font-semibold [overflow-wrap:anywhere]">
          {fact.content}
        </p>
        <div className="text-muted-foreground flex flex-wrap gap-x-2.5 gap-y-1 text-xs">
          <span>{upperFirst(fact.category)}</span>
          <span>
            {t.settings.memory.factConfidenceLabel} {confidenceText}
          </span>
          <span>{formatTimeAgo(fact.createdAt)}</span>
          <span>
            {fact.source === "manual" ? (
              t.settings.memory.manualFactSource
            ) : (
              <Link
                href={sourceThreadHref(fact)}
                className="text-selection font-medium underline-offset-4 hover:underline"
              >
                {t.settings.memory.markdown.table.view}
              </Link>
            )}
          </span>
        </div>
      </div>
      {onEdit || onDelete ? (
        <div className="flex shrink-0 items-center gap-1">
          {onEdit ? (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="shrink-0"
              onClick={onEdit}
              disabled={disabled}
              title={t.common.edit}
              aria-label={t.common.edit}
            >
              <PenLineIcon aria-hidden="true" className="size-4" />
            </Button>
          ) : null}
          {onDelete ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="shrink-0"
                  disabled={disabled}
                  title={t.common.more}
                  aria-label={t.common.more}
                >
                  <MoreHorizontalIcon aria-hidden="true" className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  variant="destructive"
                  disabled={disabled}
                  onSelect={() => {
                    window.setTimeout(onDelete, 0);
                  }}
                >
                  <Trash2Icon aria-hidden="true" />
                  {t.common.delete}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

export function MemoryFactList(props: {
  facts: MemoryFact[];
  t: Translations;
  isDeleting: boolean;
  onEdit?: (fact: MemoryFact) => void;
  onDelete?: (fact: MemoryFact) => void;
  sourceThreadHref: MemorySourceThreadHref;
  embedded?: boolean;
  showHeading?: boolean;
}): React.ReactNode {
  const {
    facts,
    t,
    isDeleting,
    onEdit,
    onDelete,
    sourceThreadHref,
    embedded = false,
    showHeading = true,
  } = props;

  return (
    <section
      data-testid="memory-facts-panel"
      aria-labelledby="memory-facts-heading"
      className={cn(
        "bg-card min-w-0 overflow-hidden",
        embedded ? "rounded-none border-0" : "rounded-xl border",
      )}
    >
      {showHeading ? (
        <div className="flex items-center justify-between gap-3 px-4 py-3 sm:px-5">
          <h2 id="memory-facts-heading" className="text-sm font-medium">
            {t.settings.memory.markdown.facts}
          </h2>
          <span className="text-muted-foreground text-xs">
            {t.settings.memory.factCount(facts.length)}
          </span>
        </div>
      ) : (
        <h2 id="memory-facts-heading" className="sr-only">
          {t.settings.memory.markdown.facts}
        </h2>
      )}
      <div className="divide-y">
        {facts.length > 0 ? (
          facts.map((fact) => (
            <MemoryFactRow
              key={fact.id}
              fact={fact}
              t={t}
              onEdit={onEdit ? () => onEdit(fact) : undefined}
              onDelete={onDelete ? () => onDelete(fact) : undefined}
              disabled={isDeleting}
              sourceThreadHref={sourceThreadHref}
            />
          ))
        ) : (
          <p className="text-muted-foreground px-4 py-5 text-sm sm:px-5">
            {t.settings.memory.noFacts}
          </p>
        )}
      </div>
    </section>
  );
}

export function MemorySummaryDisclosure(props: {
  t: Translations;
  groups: MemorySectionGroup[];
  summaryCount: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  triggerRef: React.Ref<HTMLButtonElement>;
  embedded?: boolean;
}): React.ReactNode {
  const {
    t,
    groups,
    summaryCount,
    open,
    onOpenChange,
    triggerRef,
    embedded = false,
  } = props;

  return (
    <Collapsible
      open={open}
      onOpenChange={onOpenChange}
      data-testid="memory-summary-disclosure"
      className={cn(
        "bg-card min-w-0 overflow-hidden",
        embedded ? "rounded-none border-0" : "rounded-xl border",
      )}
    >
      <CollapsibleTrigger asChild>
        <Button
          ref={triggerRef}
          variant="ghost"
          className="h-auto w-full min-w-0 justify-between rounded-none px-4 py-4 whitespace-normal sm:px-5"
          aria-controls="memory-summary-content"
        >
          <span className="flex min-w-0 flex-1 items-center gap-3 text-left">
            <SparklesIcon
              aria-hidden="true"
              className="text-muted-foreground size-4 shrink-0"
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium">
                {t.settings.memory.smartSummaries}
              </span>
              <span className="text-muted-foreground block min-w-0 text-xs [overflow-wrap:anywhere] whitespace-normal">
                {t.settings.memory.summaryCount(summaryCount)} ·{" "}
                {t.settings.memory.summaryReadOnly}
              </span>
            </span>
          </span>
          <ChevronDownIcon
            aria-hidden="true"
            className={cn("size-4 transition-transform", open && "rotate-180")}
          />
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent id="memory-summary-content">
        <div data-testid="memory-summary-panel" className="border-t p-4 sm:p-5">
          <div className="grid gap-6 lg:grid-cols-2">
            {groups.map((group) => (
              <section key={group.title} className="min-w-0 space-y-4">
                <h3 className="text-sm font-medium">{group.title}</h3>
                {group.sections.map((section) => (
                  <div
                    key={section.title}
                    className="min-w-0 border-t pt-3 first:border-t-0 first:pt-0"
                  >
                    <h4 className="text-sm font-medium">{section.title}</h4>
                    <SafeStreamdown
                      className="mt-2 min-w-0 text-sm [overflow-wrap:anywhere] [&>*:first-child]:mt-0 [&>*:last-child]:mb-0"
                      {...streamdownPlugins}
                    >
                      {section.summary.trim() ||
                        t.settings.memory.markdown.empty}
                    </SafeStreamdown>
                    {section.updatedAt ? (
                      <p className="text-muted-foreground mt-2 text-xs">
                        {t.settings.memory.markdown.updatedAt}:{" "}
                        {formatTimeAgo(section.updatedAt)}
                      </p>
                    ) : null}
                  </div>
                ))}
              </section>
            ))}
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

export function MemoryEmptyState(props: {
  t: Translations;
  onAddFact?: () => void;
}): React.ReactNode {
  const { t, onAddFact } = props;

  return (
    <section
      data-testid="memory-empty-state"
      className="flex flex-col items-center rounded-xl border border-dashed px-6 py-10 text-center"
    >
      <div className="bg-muted text-muted-foreground flex size-10 items-center justify-center rounded-full">
        <BrainIcon aria-hidden="true" className="size-5" />
      </div>
      <h2 className="mt-4 text-sm font-medium">
        {t.settings.memory.emptyTitle}
      </h2>
      <p className="text-muted-foreground mt-1 max-w-md text-sm">
        {t.settings.memory.emptyDescription}
      </p>
      {onAddFact ? (
        <Button type="button" className="mt-4" onClick={onAddFact}>
          <PlusIcon aria-hidden="true" />
          {t.settings.memory.addFact}
        </Button>
      ) : null}
    </section>
  );
}

export function MemoryLoadingState(props: { label: string }): React.ReactNode {
  const { label } = props;

  return (
    <div
      data-testid="memory-loading-state"
      role="status"
      aria-live="polite"
      aria-busy="true"
      aria-label={label}
      className="space-y-6"
    >
      <div className="bg-card grid grid-cols-2 gap-4 rounded-xl border p-4 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <div key={index} className="space-y-2">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-4 w-28 max-w-full" />
          </div>
        ))}
      </div>
      <div className="flex flex-col gap-3 rounded-xl border p-3 sm:flex-row sm:justify-between">
        <Skeleton className="h-9 w-full sm:max-w-md" />
        <Skeleton className="h-9 w-56 max-w-full" />
      </div>
      <div className="bg-card overflow-hidden rounded-xl border">
        <div className="px-4 py-3 sm:px-5">
          <Skeleton className="h-4 w-28" />
        </div>
        <div className="divide-y">
          {Array.from({ length: 3 }, (_, index) => (
            <div
              key={index}
              className="flex items-start gap-3 px-4 py-4 sm:px-5"
            >
              <Skeleton className="size-9 shrink-0" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-3 w-1/2" />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function MemoryLoadError(props: {
  t: Translations;
  error: { message: string };
}): React.ReactNode {
  const { t, error } = props;

  return (
    <Alert variant="destructive" data-testid="memory-load-error">
      <AlertCircleIcon aria-hidden="true" />
      <AlertTitle>{t.settings.memory.loadErrorTitle}</AlertTitle>
      <AlertDescription>{error.message}</AlertDescription>
    </Alert>
  );
}
