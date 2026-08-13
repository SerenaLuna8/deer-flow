"use client";

import { Loader2Icon, SearchIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import type { MemoryEpisodeTag } from "@/core/private-work/memory/types";
import { cn } from "@/lib/utils";

import { formatMemoryDate, MemoryErrorState } from "./memory-workbench-shared";
import type { MemoryEpisodesState } from "./memory-workbench-types";

export const MEMORY_EPISODE_TAGS: readonly MemoryEpisodeTag[] = [
  "permanent",
  "durable",
  "ephemeral",
  "correction",
];

export function MemoryArchivePanel({
  episodes,
}: {
  episodes: MemoryEpisodesState;
}) {
  const { locale, t } = useI18n();
  const copy = t.projectMemory;

  return (
    <section
      aria-labelledby="memory-archive-title"
      className="flex min-h-0 flex-1 flex-col gap-3"
    >
      <div className="shrink-0">
        <h2 id="memory-archive-title" className="sr-only">
          {copy.archiveTab}
        </h2>
        <p className="text-muted-foreground text-sm">
          {copy.archiveDescription}
        </p>
      </div>

      <form
        className="flex shrink-0 items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          episodes.submitSearch();
        }}
      >
        <div className="relative flex-1">
          <SearchIcon className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
          <Input
            type="search"
            value={episodes.searchInput}
            maxLength={200}
            placeholder={copy.searchPlaceholder}
            className="pl-9"
            onChange={(event) => episodes.setSearchInput(event.target.value)}
          />
        </div>
        <Button type="submit" variant="secondary">
          {copy.search}
        </Button>
      </form>

      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {MEMORY_EPISODE_TAGS.map((tag) => {
          const active = episodes.tags.includes(tag);
          return (
            <button
              key={tag}
              type="button"
              aria-pressed={active}
              className={cn(
                "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                active
                  ? "border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300"
                  : "text-muted-foreground hover:bg-muted/60",
              )}
              onClick={() => episodes.toggleTag(tag)}
            >
              {copy.tags[tag]}
            </button>
          );
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {episodes.isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-14 w-full" />
            <Skeleton className="h-14 w-full" />
          </div>
        ) : episodes.error ? (
          <MemoryErrorState
            title={copy.archiveFailed}
            retryLabel={copy.retry}
            onRetry={episodes.retry}
          />
        ) : episodes.items.length ? (
          <>
            <ul className="overflow-hidden rounded-xl border">
              {episodes.items.map((episode) => (
                <li
                  key={episode.id}
                  className="border-b px-4 py-3 last:border-b-0"
                >
                  <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                    <Badge variant="secondary">
                      {episode.origin === "snip"
                        ? copy.originSnip
                        : copy.originRemember}
                    </Badge>
                    <span>{formatMemoryDate(episode.occurredAt, locale)}</span>
                  </div>
                  <p className="mt-1.5 text-sm leading-6 whitespace-pre-wrap">
                    {episode.taggedText}
                  </p>
                </li>
              ))}
            </ul>
            {episodes.hasMore ? (
              <div className="flex justify-center py-3">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={episodes.loadingMore}
                  onClick={episodes.loadMore}
                >
                  {episodes.loadingMore ? (
                    <Loader2Icon className="size-4 animate-spin" />
                  ) : null}
                  {episodes.loadingMore ? copy.loadingMore : copy.loadMore}
                </Button>
              </div>
            ) : null}
          </>
        ) : (
          <div className="text-muted-foreground rounded-xl border border-dashed px-5 py-8 text-center text-sm">
            {episodes.activeQuery || episodes.tags.length
              ? copy.archiveNoMatch
              : copy.archiveEmpty}
          </div>
        )}
      </div>
    </section>
  );
}
