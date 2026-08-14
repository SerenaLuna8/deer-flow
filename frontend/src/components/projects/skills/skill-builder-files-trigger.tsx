"use client";

import { FilesIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/workspace/tooltip";
import { useI18n } from "@/core/i18n/hooks";

export function SkillBuilderFilesTrigger({
  fileCount,
  onOpen,
}: {
  fileCount: number;
  onOpen: () => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.files;
  if (fileCount <= 0) return null;
  return (
    <Tooltip content={copy.tooltip}>
      <Button
        type="button"
        variant="ghost"
        className="text-muted-foreground hover:text-foreground"
        aria-label={copy.aria}
        data-testid="skill-builder-files-trigger"
        onClick={onOpen}
      >
        <FilesIcon />
        <span className="hidden sm:inline">{copy.label}</span>
      </Button>
    </Tooltip>
  );
}
