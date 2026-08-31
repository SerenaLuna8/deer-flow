"use client";

import { LayersIcon, ScanSearchIcon } from "lucide-react";
import { useId, type ReactNode } from "react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useI18n } from "@/core/i18n/hooks";
import type { KnowledgeRetrievalMode } from "@/core/knowledge/types";
import { cn } from "@/lib/utils";

/** The saved base default, distinct from the retrieval test's one-off override. */
export function KnowledgeRetrievalModeField({
  value,
  onChange,
  disabled = false,
  variant = "select",
  showLabel = true,
  showHint = true,
  selectedContent,
}: {
  value: KnowledgeRetrievalMode;
  onChange: (value: KnowledgeRetrievalMode) => void;
  disabled?: boolean;
  variant?: "select" | "cards";
  showLabel?: boolean;
  showHint?: boolean;
  selectedContent?: ReactNode;
}) {
  const { t } = useI18n();
  const labels = t.knowledge.bases;
  const groupId = useId();

  if (variant === "cards") {
    return (
      <fieldset
        role="radiogroup"
        aria-label={labels.retrievalModeLabel}
        disabled={disabled}
        className="min-w-0 space-y-2 text-[13px]"
      >
        {showLabel ? (
          <legend className="mb-2 font-medium">
            {labels.retrievalModeLabel}
          </legend>
        ) : null}
        {showHint ? (
          <p className="text-muted-foreground text-xs leading-5">
            {labels.retrievalModeHint}
          </p>
        ) : null}
        {(["semantic", "hybrid"] as const).map((mode) => {
          const selected = value === mode;
          const Icon = mode === "semantic" ? ScanSearchIcon : LayersIcon;
          return (
            <div
              key={mode}
              className={cn(
                "min-w-0 overflow-hidden rounded-xl border transition-colors focus-within:ring-2 focus-within:ring-blue-500/20",
                selected
                  ? "border-blue-600 bg-blue-50/30 dark:border-blue-400 dark:bg-blue-950/20"
                  : "border-border/70 bg-muted/20 hover:bg-muted/40",
                disabled && "cursor-default opacity-60",
              )}
            >
              <label className="flex cursor-pointer items-start gap-3 p-3.5">
                <span className="border-border/50 bg-background inline-flex size-8 shrink-0 items-center justify-center rounded-lg border text-blue-600 shadow-xs dark:text-blue-400">
                  <Icon aria-hidden className="size-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block font-semibold">
                    {labels.retrievalModes[mode]}
                  </span>
                  <span className="text-muted-foreground mt-0.5 block text-xs leading-5">
                    {labels.retrievalModeDescriptions[mode]}
                  </span>
                </span>
                <input
                  type="radio"
                  name={groupId}
                  value={mode}
                  aria-label={labels.retrievalModes[mode]}
                  checked={selected}
                  onChange={() => onChange(mode)}
                  className="mt-0.5 size-4 shrink-0 accent-blue-600"
                />
              </label>
              {selected && selectedContent ? (
                <div className="border-border/60 bg-background border-t p-3.5">
                  {selectedContent}
                </div>
              ) : null}
            </div>
          );
        })}
      </fieldset>
    );
  }

  return (
    <div className="grid gap-1.5 text-[13px]">
      {showLabel ? (
        <span className="font-medium">{labels.retrievalModeLabel}</span>
      ) : null}
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
      {showHint ? (
        <p className="text-muted-foreground text-xs leading-5">
          {labels.retrievalModeHint}
        </p>
      ) : null}
    </div>
  );
}
