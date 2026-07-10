"use client";

import {
  AlertCircleIcon,
  BrainIcon,
  BriefcaseBusinessIcon,
  ChevronDownIcon,
  Clock3Icon,
  DownloadIcon,
  FileTextIcon,
  FolderGit2Icon,
  HeartIcon,
  Layers3Icon,
  NotebookTextIcon,
  PenLineIcon,
  PlusIcon,
  SearchIcon,
  Settings2Icon,
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
  type MemoryFact,
  type MemorySectionGroup,
  type MemoryViewFilter,
  upperFirst,
} from "@/components/workspace/settings/memory/memory-view-model";
import type { Translations } from "@/core/i18n/locales/types";
import { SafeStreamdown } from "@/core/streamdown/components";
import { streamdownPlugins } from "@/core/streamdown/plugins";
import { pathOfThread } from "@/core/threads/utils";
import { formatTimeAgo } from "@/core/utils/datetime";
import { cn } from "@/lib/utils";

export function MemoryHeaderActions(props: {
  t: Translations;
  isImporting: boolean;
  isExporting: boolean;
  isClearing: boolean;
  onAddFact: () => void;
  onImport: () => void;
  onExport: () => void;
  onClear: () => void;
}): React.ReactNode {
  const {
    t,
    isImporting,
    isExporting,
    isClearing,
    onAddFact,
    onImport,
    onExport,
    onClear,
  } = props;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline">
            <Settings2Icon aria-hidden="true" />
            {t.settings.memory.manageMemory}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-52">
          <DropdownMenuItem disabled={isImporting} onSelect={onImport}>
            <UploadIcon aria-hidden="true" />
            {t.settings.memory.importButton}
          </DropdownMenuItem>
          <DropdownMenuItem disabled={isExporting} onSelect={onExport}>
            <DownloadIcon aria-hidden="true" />
            {isExporting ? t.common.loading : t.settings.memory.exportButton}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            variant="destructive"
            disabled={isClearing}
            onSelect={onClear}
          >
            <Trash2Icon aria-hidden="true" />
            {isClearing ? t.common.loading : t.settings.memory.clearAll}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Button onClick={onAddFact}>
        <PlusIcon aria-hidden="true" />
        {t.settings.memory.addFact}
      </Button>
    </div>
  );
}

export function MemoryOverview(props: {
  t: Translations;
  factCount: number;
  summaryCount: number;
  lastUpdated: string;
  recentFocus: string;
  onViewSummaries: () => void;
}): React.ReactNode {
  const {
    t,
    factCount,
    summaryCount,
    lastUpdated,
    recentFocus,
    onViewSummaries,
  } = props;

  return (
    <section
      data-testid="memory-overview"
      className="bg-card rounded-xl border"
    >
      <div className="grid grid-cols-2 lg:grid-cols-[minmax(0,0.7fr)_minmax(0,0.8fr)_minmax(0,1fr)_minmax(0,2fr)]">
        <div className="border-border/70 flex items-start gap-3 border-r border-b p-4 lg:border-b-0">
          <FileTextIcon
            aria-hidden="true"
            className="text-muted-foreground mt-0.5 size-4"
          />
          <p className="text-sm font-medium">
            {t.settings.memory.factCount(factCount)}
          </p>
        </div>

        <div className="border-border/70 flex items-start gap-3 border-b p-4 lg:border-r lg:border-b-0">
          <Layers3Icon
            aria-hidden="true"
            className="text-muted-foreground mt-0.5 size-4"
          />
          <p className="text-sm font-medium">
            {t.settings.memory.summaryCount(summaryCount)}
          </p>
        </div>

        <div className="border-border/70 flex min-w-0 items-start gap-3 border-r p-4">
          <Clock3Icon
            aria-hidden="true"
            className="text-muted-foreground mt-0.5 size-4"
          />
          <div className="min-w-0">
            <p className="text-muted-foreground text-xs">
              {t.common.lastUpdated}
            </p>
            <p className="truncate text-sm font-medium">{lastUpdated}</p>
          </div>
        </div>

        <div className="flex min-w-0 items-start gap-3 p-4">
          <StarIcon
            aria-hidden="true"
            className="text-muted-foreground mt-0.5 size-4"
          />
          <div className="min-w-0 flex-1">
            <p className="text-muted-foreground text-xs">
              {t.settings.memory.recentFocus}
            </p>
            <p className="line-clamp-2 text-sm font-medium [overflow-wrap:anywhere]">
              {recentFocus}
            </p>
            <Button
              type="button"
              variant="link"
              size="sm"
              className="h-auto px-0 py-0 text-xs"
              onClick={onViewSummaries}
            >
              {t.settings.memory.viewSummaries}
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}

export function MemoryToolbar(props: {
  t: Translations;
  query: string;
  filter: MemoryViewFilter;
  onQueryChange: (query: string) => void;
  onFilterChange: (filter: MemoryViewFilter) => void;
}): React.ReactNode {
  const { t, query, filter, onQueryChange, onFilterChange } = props;

  return (
    <div className="bg-muted/25 flex flex-col gap-3 rounded-xl border p-3 sm:flex-row sm:items-center">
      <div className="relative min-w-0 flex-1 sm:max-w-md">
        <SearchIcon
          aria-hidden="true"
          className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
        />
        <Input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder={t.settings.memory.searchPlaceholder}
          className="pl-9"
        />
      </div>
      <ToggleGroup
        type="single"
        value={filter}
        onValueChange={(value) => {
          if (value) onFilterChange(value as MemoryViewFilter);
        }}
        variant="outline"
        className="max-w-full shrink-0 self-start sm:ml-auto sm:self-auto"
      >
        <ToggleGroupItem
          data-testid="memory-filter-all"
          value="all"
          className="whitespace-nowrap"
        >
          {t.settings.memory.filterAll}
        </ToggleGroupItem>
        <ToggleGroupItem
          data-testid="memory-filter-facts"
          value="facts"
          className="whitespace-nowrap"
        >
          {t.settings.memory.filterFacts}
        </ToggleGroupItem>
        <ToggleGroupItem
          data-testid="memory-filter-summaries"
          value="summaries"
          className="whitespace-nowrap"
        >
          {t.settings.memory.filterSummaries}
        </ToggleGroupItem>
      </ToggleGroup>
    </div>
  );
}

function MemoryFactRow(props: {
  fact: MemoryFact;
  t: Translations;
  onEdit: () => void;
  onDelete: () => void;
  disabled: boolean;
}): React.ReactNode {
  const { fact, t, onEdit, onDelete, disabled } = props;
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
    <article className="hover:bg-muted/30 flex min-w-0 items-start gap-3 px-4 py-4 [overflow-wrap:anywhere] transition-colors sm:px-5">
      <div className="bg-muted/50 text-muted-foreground mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-lg border">
        <CategoryIcon aria-hidden="true" className="size-4" />
      </div>
      <div className="min-w-0 flex-1 space-y-2">
        <p className="text-sm font-medium [overflow-wrap:anywhere]">
          {fact.content}
        </p>
        <div className="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 text-xs">
          <span>{upperFirst(fact.category)}</span>
          <span>{confidenceText}</span>
          <span>{formatTimeAgo(fact.createdAt)}</span>
          <span>
            {fact.source === "manual" ? (
              t.settings.memory.manualFactSource
            ) : (
              <Link
                href={pathOfThread(fact.source)}
                className="text-primary font-medium underline-offset-4 hover:underline"
              >
                {t.settings.memory.markdown.table.view}
              </Link>
            )}
          </span>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
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
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="text-destructive hover:text-destructive shrink-0"
          onClick={onDelete}
          disabled={disabled}
          title={t.common.delete}
          aria-label={t.common.delete}
        >
          <Trash2Icon aria-hidden="true" className="size-4" />
        </Button>
      </div>
    </article>
  );
}

export function MemoryFactList(props: {
  facts: MemoryFact[];
  t: Translations;
  isDeleting: boolean;
  onEdit: (fact: MemoryFact) => void;
  onDelete: (fact: MemoryFact) => void;
}): React.ReactNode {
  const { facts, t, isDeleting, onEdit, onDelete } = props;

  return (
    <section
      data-testid="memory-facts-panel"
      aria-labelledby="memory-facts-heading"
      className="bg-card min-w-0 overflow-hidden rounded-xl border"
    >
      <div className="flex items-center justify-between gap-3 px-4 py-3 sm:px-5">
        <h2 id="memory-facts-heading" className="text-sm font-medium">
          {t.settings.memory.markdown.facts}
        </h2>
        <span className="text-muted-foreground text-xs">
          {t.settings.memory.factCount(facts.length)}
        </span>
      </div>
      <div className="divide-y">
        {facts.length > 0 ? (
          facts.map((fact) => (
            <MemoryFactRow
              key={fact.id}
              fact={fact}
              t={t}
              onEdit={() => onEdit(fact)}
              onDelete={() => onDelete(fact)}
              disabled={isDeleting}
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
}): React.ReactNode {
  const { t, groups, summaryCount, open, onOpenChange, triggerRef } = props;

  return (
    <Collapsible
      open={open}
      onOpenChange={onOpenChange}
      data-testid="memory-summary-disclosure"
      className="bg-card min-w-0 overflow-hidden rounded-xl border"
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
  onAddFact: () => void;
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
      <Button type="button" className="mt-4" onClick={onAddFact}>
        <PlusIcon aria-hidden="true" />
        {t.settings.memory.addFact}
      </Button>
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
