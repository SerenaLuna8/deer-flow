"use client";

import {
  Clock3Icon,
  DownloadIcon,
  FileTextIcon,
  Layers3Icon,
  PlusIcon,
  SearchIcon,
  Settings2Icon,
  StarIcon,
  Trash2Icon,
  UploadIcon,
} from "lucide-react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { MemoryViewFilter } from "@/components/workspace/settings/memory/memory-view-model";
import type { Translations } from "@/core/i18n/locales/types";

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
