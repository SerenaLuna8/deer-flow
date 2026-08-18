import { FolderPlusIcon, SearchXIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";

export function ProjectEmptyState({
  search,
  filtered,
  onClearFilter,
  onClearSearch,
}: {
  search: string;
  filtered: boolean;
  onClearFilter: () => void;
  onClearSearch: () => void;
}) {
  const { t } = useI18n();
  const copy = t.projectWorkspace.empty;
  const searching = search.trim().length > 0;
  const narrowed = searching || filtered;
  return (
    <div
      data-testid={narrowed ? "project-search-empty" : "project-empty"}
      className="border-border/70 bg-muted/20 flex min-h-80 flex-col items-center justify-center rounded-2xl border border-dashed px-6 text-center"
    >
      {narrowed ? (
        <SearchXIcon className="text-muted-foreground mb-4 size-10" />
      ) : (
        <FolderPlusIcon className="text-primary mb-4 size-10" />
      )}
      <h2 className="text-xl font-semibold">
        {narrowed ? copy.noMatchesTitle : copy.firstProjectTitle}
      </h2>
      <p className="text-muted-foreground mt-2 max-w-md text-sm">
        {narrowed ? copy.noMatchesDescription : copy.firstProjectDescription}
      </p>
      {narrowed ? (
        <Button
          type="button"
          className="mt-6"
          variant="outline"
          onClick={() => {
            onClearSearch();
            onClearFilter();
          }}
        >
          {copy.clearFilters}
        </Button>
      ) : null}
    </div>
  );
}
