import { XIcon } from "lucide-react";

import { Suggestion, Suggestions } from "@/components/ai-elements/suggestion";
import { Button } from "@/components/ui/button";

export function FollowupSuggestions({
  suggestions,
  loading,
  loadingLabel,
  closeLabel,
  onSelect,
  onClose,
}: {
  suggestions: string[];
  loading: boolean;
  loadingLabel: string;
  closeLabel: string;
  onSelect: (suggestion: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="flex items-center justify-center pb-1">
      <div className="flex items-center gap-2">
        {loading ? (
          <div className="text-muted-foreground bg-background/80 rounded-full border px-4 py-1.5 text-xs backdrop-blur-sm">
            {loadingLabel}
          </div>
        ) : (
          <Suggestions className="w-fit items-center">
            {suggestions.map((suggestion) => (
              <Suggestion
                key={suggestion}
                className="py-1.5"
                suggestion={suggestion}
                onClick={() => onSelect(suggestion)}
              />
            ))}
            <Button
              aria-label={closeLabel}
              className="text-muted-foreground h-auto cursor-pointer rounded-full px-2.5 py-1.5 text-xs font-normal"
              variant="outline"
              size="sm"
              type="button"
              onClick={onClose}
            >
              <XIcon className="size-4" />
            </Button>
          </Suggestions>
        )}
      </div>
    </div>
  );
}
