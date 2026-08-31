"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useI18n } from "@/core/i18n/hooks";
import type { KnowledgeRetrievalMode } from "@/core/knowledge/types";

/** The saved base default, distinct from the retrieval test's one-off override. */
export function KnowledgeRetrievalModeField({
  value,
  onChange,
  disabled = false,
}: {
  value: KnowledgeRetrievalMode;
  onChange: (value: KnowledgeRetrievalMode) => void;
  disabled?: boolean;
}) {
  const { t } = useI18n();
  const labels = t.knowledge.bases;
  return (
    <div className="grid gap-1.5 text-[13px]">
      <span className="font-medium">{labels.retrievalModeLabel}</span>
      <Select
        value={value}
        disabled={disabled}
        onValueChange={(next) => {
          if (next === "semantic" || next === "hybrid") onChange(next);
        }}
      >
        <SelectTrigger
          className="border-border/70 bg-background focus-visible:border-selection/50 focus-visible:ring-selection/15 rounded-lg text-[13px] shadow-none focus-visible:ring-2"
          aria-label={labels.retrievalModeLabel}
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent className="border-border/70 rounded-lg">
          <SelectItem className="rounded-md text-[13px]" value="semantic">
            {labels.retrievalModes.semantic}
          </SelectItem>
          <SelectItem className="rounded-md text-[13px]" value="hybrid">
            {labels.retrievalModes.hybrid}
          </SelectItem>
        </SelectContent>
      </Select>
      <p className="text-muted-foreground text-xs leading-5">
        {labels.retrievalModeHint}
      </p>
    </div>
  );
}
