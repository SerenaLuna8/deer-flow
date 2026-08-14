import { CheckIcon } from "lucide-react";

import { PromptInputButton } from "@/components/ai-elements/prompt-input";
import type { Model } from "@/core/models/types";
import { cn } from "@/lib/utils";

import {
  ModelSelector,
  ModelSelectorContent,
  ModelSelectorItem,
  ModelSelectorLabel,
  ModelSelectorList,
  ModelSelectorName,
  ModelSelectorTrigger,
} from "./model-selector-popover";
import { Tooltip } from "./tooltip";

type ModelChooserLabels = {
  agentModelLocked: string;
  model: string;
};

export function InputBoxModelChooser({
  models,
  selectedModelName,
  displayName,
  locked,
  open,
  disabled,
  labels,
  onOpenChange,
  onSelect,
}: {
  models: readonly Model[];
  selectedModelName: string | undefined;
  displayName: string | undefined;
  locked: boolean;
  open: boolean;
  disabled: boolean;
  labels: ModelChooserLabels;
  onOpenChange: (open: boolean) => void;
  onSelect: (modelName: string) => void;
}) {
  return locked ? (
    <Tooltip content={labels.agentModelLocked}>
      <PromptInputButton
        className="max-w-40 min-w-0 sm:max-w-56"
        data-testid="agent-model-locked"
        disabled
      >
        <div className="flex min-w-0 flex-col items-start text-left">
          <ModelSelectorName className="text-xs font-normal">
            {displayName}
          </ModelSelectorName>
        </div>
      </PromptInputButton>
    </Tooltip>
  ) : (
    <ModelSelector open={open} onOpenChange={onOpenChange}>
      <ModelSelectorTrigger asChild>
        <PromptInputButton
          className="max-w-40 min-w-0 sm:max-w-56"
          disabled={disabled}
        >
          <div className="flex min-w-0 flex-col items-start text-left">
            <ModelSelectorName className="text-xs font-normal">
              {displayName}
            </ModelSelectorName>
          </div>
        </PromptInputButton>
      </ModelSelectorTrigger>
      <ModelSelectorContent>
        <ModelSelectorLabel>{labels.model}</ModelSelectorLabel>
        <ModelSelectorList>
          {models.map((model) => (
            <ModelSelectorItem
              className={cn(
                model.name === selectedModelName
                  ? "text-accent-foreground"
                  : "text-muted-foreground/65",
              )}
              key={model.name}
              onSelect={() => onSelect(model.name)}
            >
              <div className="flex min-w-0 flex-1 flex-col">
                <ModelSelectorName>{model.display_name}</ModelSelectorName>
                <span className="text-muted-foreground truncate text-xs">
                  {model.model}
                </span>
              </div>
              {model.name === selectedModelName ? (
                <CheckIcon className="ml-auto size-4" />
              ) : (
                <div className="ml-auto size-4" />
              )}
            </ModelSelectorItem>
          ))}
        </ModelSelectorList>
      </ModelSelectorContent>
    </ModelSelector>
  );
}
