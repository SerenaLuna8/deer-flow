import { FolderPlusIcon, SearchXIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

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
        {narrowed ? "没有匹配的项目" : "创建你的第一个项目"}
      </h2>
      <p className="text-muted-foreground mt-2 max-w-md text-sm">
        {narrowed
          ? "换一个关键词，或清除筛选查看全部项目。"
          : "项目用于组织成员和共享的 Agent、Skill 与 MCP。"}
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
          清除筛选
        </Button>
      ) : null}
    </div>
  );
}
