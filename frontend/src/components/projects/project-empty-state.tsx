import { FolderPlusIcon, SearchXIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";

export function ProjectEmptyState({
  search,
  onClearSearch,
}: {
  search: string;
  onClearSearch: () => void;
}) {
  const { t } = useI18n();
  const copy = t.projectWorkspace.empty;
  const searching = search.trim().length > 0;
  return (
    <div
      data-testid={searching ? "project-search-empty" : "project-empty"}
      className="border-border/70 bg-card/60 flex min-h-48 flex-col items-center justify-center rounded-xl border border-dashed px-5 py-6 text-center"
    >
      {searching ? (
        <SearchXIcon
          aria-hidden
          className="text-muted-foreground mb-3 size-6"
        />
      ) : (
        <FolderPlusIcon aria-hidden className="text-primary mb-3 size-6" />
      )}
      <h2 className="text-sm font-semibold">
        {searching ? copy.noMatchesTitle : copy.firstProjectTitle}
      </h2>
      <p className="text-muted-foreground mt-1.5 max-w-sm text-[13px] leading-5">
        {searching ? copy.noMatchesDescription : copy.firstProjectDescription}
      </p>
      {searching ? (
        <Button
          type="button"
          className="mt-4 text-xs"
          size="sm"
          variant="outline"
          onClick={onClearSearch}
        >
          {copy.clearFilters}
        </Button>
      ) : null}
    </div>
  );
}
