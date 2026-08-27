import { SparklesIcon, TargetIcon } from "lucide-react";

import { cn } from "@/lib/utils";

import type { SlashSuggestion } from "./input-box-helpers";

export function SlashSkillSuggestionsListbox({
  suggestions,
  selectedIndex,
  onApply,
  onHighlight,
}: {
  suggestions: SlashSuggestion[];
  selectedIndex: number;
  onApply: (suggestion: SlashSuggestion) => void;
  onHighlight: (index: number) => void;
}) {
  return (
    <div className="absolute right-0 bottom-full left-0 z-40 mb-2 px-1">
      <div
        aria-label="Skill suggestions"
        className="bg-popover/95 text-popover-foreground border-border max-h-72 overflow-y-auto rounded-xl border p-1 shadow-lg backdrop-blur-sm"
        role="listbox"
      >
        {suggestions.map((suggestion, index) => {
          const selected = index === selectedIndex;
          return (
            <button
              aria-selected={selected}
              className={cn(
                "flex min-h-12 w-full min-w-0 cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors",
                selected
                  ? "bg-accent text-accent-foreground"
                  : "text-popover-foreground hover:bg-accent/70 hover:text-accent-foreground",
              )}
              key={`${suggestion.kind}:${suggestion.name}`}
              onClick={() => onApply(suggestion)}
              onMouseDown={(event) => event.preventDefault()}
              onMouseMove={() => onHighlight(index)}
              role="option"
              type="button"
            >
              {suggestion.kind === "builtin" ? (
                <TargetIcon className="text-muted-foreground size-4 shrink-0" />
              ) : (
                <SparklesIcon className="text-muted-foreground size-4 shrink-0" />
              )}
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">
                  /{suggestion.name}
                </span>
                {suggestion.description && (
                  <span className="text-muted-foreground block truncate text-xs">
                    {suggestion.description}
                  </span>
                )}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
