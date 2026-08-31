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
    <div className="grid gap-1.5 text-sm">
      <span className="font-medium">{labels.retrievalModeLabel}</span>
      <Select
        value={value}
        disabled={disabled}
        onValueChange={(next) => {
          if (next === "semantic" || next === "hybrid") onChange(next);
        }}
      >
        <SelectTrigger aria-label={labels.retrievalModeLabel}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="semantic">
            {labels.retrievalModes.semantic}
          </SelectItem>
          <SelectItem value="hybrid">{labels.retrievalModes.hybrid}</SelectItem>
        </SelectContent>
      </Select>
      <p className="text-muted-foreground text-xs">
        {labels.retrievalModeHint}
      </p>
    </div>
  );
}
