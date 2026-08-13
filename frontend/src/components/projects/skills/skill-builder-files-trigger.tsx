"use client";

import { FilesIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/workspace/tooltip";

export function SkillBuilderFilesTrigger({
  fileCount,
  onOpen,
}: {
  fileCount: number;
  onOpen: () => void;
}) {
  if (fileCount <= 0) return null;
  return (
    <Tooltip content="查看候选文件包">
      <Button
        type="button"
        variant="ghost"
        className="text-muted-foreground hover:text-foreground"
        aria-label="查看候选文件包"
        data-testid="skill-builder-files-trigger"
        onClick={onOpen}
      >
        <FilesIcon />
        <span className="hidden sm:inline">文件</span>
      </Button>
    </Tooltip>
  );
}
