import {
  CheckIcon,
  GraduationCapIcon,
  LightbulbIcon,
  RocketIcon,
  ZapIcon,
} from "lucide-react";

import {
  PromptInputActionMenu,
  PromptInputActionMenuContent,
  PromptInputActionMenuItem,
  PromptInputActionMenuTrigger,
} from "@/components/ai-elements/prompt-input";
import {
  DropdownMenuGroup,
  DropdownMenuLabel,
} from "@/components/ui/dropdown-menu";
import type { AgentMode } from "@/core/threads/agent-mode";
import { cn } from "@/lib/utils";

import { ModeHoverGuide } from "./mode-hover-guide";

type ModeChooserLabels = {
  mode: string;
  flashMode: string;
  flashModeDescription: string;
  reasoningMode: string;
  reasoningModeDescription: string;
  proMode: string;
  proModeDescription: string;
  ultraMode: string;
  ultraModeDescription: string;
};

export function InputBoxModeChooser({
  mode,
  disabled,
  supportThinking,
  supportReasoningEffort,
  labels,
  onSelect,
}: {
  mode: AgentMode;
  disabled: boolean;
  supportThinking: boolean;
  supportReasoningEffort: boolean;
  labels: ModeChooserLabels;
  onSelect: (mode: AgentMode) => void;
}) {
  return (
    <PromptInputActionMenu>
      <ModeHoverGuide mode={mode}>
        <PromptInputActionMenuTrigger
          className="max-w-28 gap-1! px-2! sm:max-w-none"
          disabled={disabled}
        >
          <div>
            {mode === "flash" && <ZapIcon className="size-3" />}
            {mode === "thinking" && <LightbulbIcon className="size-3" />}
            {mode === "pro" && <GraduationCapIcon className="size-3" />}
            {mode === "ultra" && (
              <RocketIcon className="size-3 text-[#dabb5e]" />
            )}
          </div>
          <div
            className={cn(
              "truncate text-xs font-normal",
              mode === "ultra" ? "golden-text" : "",
            )}
          >
            {(mode === "flash" && labels.flashMode) ||
              (mode === "thinking" && labels.reasoningMode) ||
              (mode === "pro" && labels.proMode) ||
              (mode === "ultra" && labels.ultraMode)}
          </div>
        </PromptInputActionMenuTrigger>
      </ModeHoverGuide>
      <PromptInputActionMenuContent className="w-80">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="text-muted-foreground text-xs">
            {labels.mode}
          </DropdownMenuLabel>
          <PromptInputActionMenu>
            <PromptInputActionMenuItem
              className={cn(
                mode === "flash"
                  ? "text-accent-foreground"
                  : "text-muted-foreground/65",
              )}
              onSelect={() => onSelect("flash")}
            >
              <div className="flex flex-col gap-2">
                <div className="flex items-center gap-1 font-bold">
                  <ZapIcon
                    className={cn(
                      "mr-2 size-4",
                      mode === "flash" && "text-accent-foreground",
                    )}
                  />
                  {labels.flashMode}
                </div>
                <div className="pl-7 text-xs">
                  {labels.flashModeDescription}
                </div>
              </div>
              {mode === "flash" ? (
                <CheckIcon className="ml-auto size-4" />
              ) : (
                <div className="ml-auto size-4" />
              )}
            </PromptInputActionMenuItem>
            {supportThinking && (
              <>
                <PromptInputActionMenuItem
                  className={cn(
                    mode === "thinking"
                      ? "text-accent-foreground"
                      : "text-muted-foreground/65",
                  )}
                  onSelect={() => onSelect("thinking")}
                >
                  <div className="flex flex-col gap-2">
                    <div className="flex items-center gap-1 font-bold">
                      <LightbulbIcon
                        className={cn(
                          "mr-2 size-4",
                          mode === "thinking" && "text-accent-foreground",
                        )}
                      />
                      {labels.reasoningMode}
                    </div>
                    <div className="pl-7 text-xs">
                      {labels.reasoningModeDescription}
                    </div>
                  </div>
                  {mode === "thinking" ? (
                    <CheckIcon className="ml-auto size-4" />
                  ) : (
                    <div className="ml-auto size-4" />
                  )}
                </PromptInputActionMenuItem>
                {supportReasoningEffort && (
                  <>
                    <PromptInputActionMenuItem
                      className={cn(
                        mode === "pro"
                          ? "text-accent-foreground"
                          : "text-muted-foreground/65",
                      )}
                      onSelect={() => onSelect("pro")}
                    >
                      <div className="flex flex-col gap-2">
                        <div className="flex items-center gap-1 font-bold">
                          <GraduationCapIcon
                            className={cn(
                              "mr-2 size-4",
                              mode === "pro" && "text-accent-foreground",
                            )}
                          />
                          {labels.proMode}
                        </div>
                        <div className="pl-7 text-xs">
                          {labels.proModeDescription}
                        </div>
                      </div>
                      {mode === "pro" ? (
                        <CheckIcon className="ml-auto size-4" />
                      ) : (
                        <div className="ml-auto size-4" />
                      )}
                    </PromptInputActionMenuItem>
                    <PromptInputActionMenuItem
                      className={cn(
                        mode === "ultra"
                          ? "text-accent-foreground"
                          : "text-muted-foreground/65",
                      )}
                      onSelect={() => onSelect("ultra")}
                    >
                      <div className="flex flex-col gap-2">
                        <div className="flex items-center gap-1 font-bold">
                          <RocketIcon
                            className={cn(
                              "mr-2 size-4",
                              mode === "ultra" && "text-[#dabb5e]",
                            )}
                          />
                          <div
                            className={cn(mode === "ultra" && "golden-text")}
                          >
                            {labels.ultraMode}
                          </div>
                        </div>
                        <div className="pl-7 text-xs">
                          {labels.ultraModeDescription}
                        </div>
                      </div>
                      {mode === "ultra" ? (
                        <CheckIcon className="ml-auto size-4" />
                      ) : (
                        <div className="ml-auto size-4" />
                      )}
                    </PromptInputActionMenuItem>
                  </>
                )}
              </>
            )}
          </PromptInputActionMenu>
        </DropdownMenuGroup>
      </PromptInputActionMenuContent>
    </PromptInputActionMenu>
  );
}
