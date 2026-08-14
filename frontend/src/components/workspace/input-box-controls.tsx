"use client";

import {
  MicIcon,
  PaperclipIcon,
  PlusIcon,
  SparklesIcon,
  SquareIcon,
} from "lucide-react";
import { useCallback } from "react";

import {
  PromptInputButton,
  usePromptInputAttachments,
  usePromptInputController,
} from "@/components/ai-elements/prompt-input";
import { Suggestion, Suggestions } from "@/components/ai-elements/suggestion";
import { ConfettiButton } from "@/components/ui/confetti-button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useI18n } from "@/core/i18n/hooks";
import { formatUploadSize, type UploadLimits } from "@/core/uploads";
import { getVoiceInputButtonState } from "@/core/voice-input/interaction";
import { cn } from "@/lib/utils";

import { Tooltip } from "./tooltip";

export function SuggestionList({
  onSelectPlaceholder,
}: {
  onSelectPlaceholder: (newText: string) => void;
}) {
  const { t } = useI18n();
  const { textInput } = usePromptInputController();
  const handleSuggestionClick = useCallback(
    (prompt: string | undefined) => {
      if (!prompt) return;
      textInput.setInput(prompt);
      onSelectPlaceholder(prompt);
    },
    [textInput, onSelectPlaceholder],
  );
  return (
    <Suggestions className="w-full max-w-full justify-center px-4 sm:w-fit sm:px-0">
      <ConfettiButton
        className="text-muted-foreground cursor-pointer rounded-full px-4 text-xs font-normal"
        variant="outline"
        size="sm"
        onClick={() => handleSuggestionClick(t.inputBox.surpriseMePrompt)}
      >
        <SparklesIcon className="size-4" /> {t.inputBox.surpriseMe}
      </ConfettiButton>
      {t.inputBox.suggestions.map((suggestion) => (
        <Suggestion
          key={suggestion.suggestion}
          icon={suggestion.icon}
          suggestion={suggestion.suggestion}
          onClick={() => handleSuggestionClick(suggestion.prompt)}
        />
      ))}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Suggestion icon={PlusIcon} suggestion={t.common.create} />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuGroup>
            {t.inputBox.suggestionsCreate.map((suggestion, index) =>
              "type" in suggestion && suggestion.type === "separator" ? (
                <DropdownMenuSeparator key={index} />
              ) : (
                !("type" in suggestion) && (
                  <DropdownMenuItem
                    key={suggestion.suggestion}
                    onClick={() => handleSuggestionClick(suggestion.prompt)}
                  >
                    {suggestion.icon && <suggestion.icon className="size-4" />}
                    {suggestion.suggestion}
                  </DropdownMenuItem>
                )
              ),
            )}
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </Suggestions>
  );
}

export function AddAttachmentsButton({
  className,
  disabled,
  uploadLimits,
}: {
  className?: string;
  disabled?: boolean;
  uploadLimits?: UploadLimits;
}) {
  const { t } = useI18n();
  const attachments = usePromptInputAttachments();
  const tooltipContent = uploadLimits
    ? t.uploads.limitsHint(
        uploadLimits.max_files,
        formatUploadSize(uploadLimits.max_file_size),
        formatUploadSize(uploadLimits.max_total_size),
      )
    : t.inputBox.addAttachments;
  return (
    <Tooltip content={<span className="block max-w-80">{tooltipContent}</span>}>
      <PromptInputButton
        aria-label={t.inputBox.addAttachments}
        className={cn("px-2!", className)}
        data-testid="add-attachments-button"
        disabled={disabled}
        onClick={() => attachments.openFileDialog()}
      >
        <PaperclipIcon className="size-3" />
      </PromptInputButton>
    </Tooltip>
  );
}

export function VoiceInputButton({
  disabled,
  listening,
  supported,
  onToggle,
}: {
  disabled?: boolean;
  listening: boolean;
  supported: boolean;
  onToggle: () => void;
}) {
  const { t } = useI18n();
  const tooltipContent = !supported
    ? t.inputBox.voiceInputUnsupported
    : listening
      ? t.inputBox.voiceInputListening
      : t.inputBox.voiceInputStart;
  const label = listening
    ? t.inputBox.voiceInputStopLabel
    : t.inputBox.voiceInputStartLabel;
  const buttonState = getVoiceInputButtonState({
    composerDisabled: disabled ?? false,
    supported,
  });

  return (
    <Tooltip content={<span className="block max-w-72">{tooltipContent}</span>}>
      <PromptInputButton
        aria-label={label}
        aria-disabled={buttonState.ariaDisabled}
        aria-pressed={listening}
        className={cn(
          "px-2!",
          listening && "text-primary bg-primary/10 hover:bg-primary/15",
          buttonState.visuallyDisabled &&
            "cursor-not-allowed opacity-50 hover:bg-transparent dark:hover:bg-transparent",
        )}
        data-testid="voice-input-button"
        disabled={buttonState.nativeDisabled}
        onClick={onToggle}
      >
        {listening ? (
          <SquareIcon className="size-3 fill-current" />
        ) : (
          <MicIcon className="size-3" />
        )}
      </PromptInputButton>
    </Tooltip>
  );
}
