import { FolderPlusIcon, SearchXIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

export function ProjectEmptyState({
  search,
  onCreate,
  onClearSearch,
}: {
  search: string;
  onCreate: () => void;
  onClearSearch: () => void;
}) {
  const searching = search.trim().length > 0;
  return (
    <div
      data-testid={searching ? "project-search-empty" : "project-empty"}
      className="border-border/70 bg-muted/20 flex min-h-80 flex-col items-center justify-center rounded-2xl border border-dashed px-6 text-center"
    >
      {searching ? (
        <SearchXIcon className="text-muted-foreground mb-4 size-10" />
      ) : (
        <FolderPlusIcon className="text-primary mb-4 size-10" />
      )}
      <h2 className="text-xl font-semibold">
        {searching ? "没有匹配的项目" : "创建你的第一个项目"}
      </h2>
      <p className="text-muted-foreground mt-2 max-w-md text-sm">
        {searching
          ? "换一个关键词，或清除搜索查看全部项目。"
          : "项目用于组织成员和共享的 Agent、Skill 与 MCP。"}
      </p>
      <Button
        type="button"
        className="mt-6"
        variant={searching ? "outline" : "default"}
        onClick={searching ? onClearSearch : onCreate}
      >
        {searching ? "清除搜索" : "创建项目"}
      </Button>
    </div>
  );
}
