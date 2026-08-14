"use client";

import {
  CheckIcon,
  ChevronDownIcon,
  FileTextIcon,
  LightbulbIcon,
  PaperclipIcon,
  RocketIcon,
  TargetIcon,
  XIcon,
  ZapIcon,
} from "lucide-react";
import { useRef, type ComponentType } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useI18n } from "@/core/i18n/hooks";
import type { Model } from "@/core/models/types";
import type { SkillBuilderAttachment } from "@/core/skill-builder";
import type { AgentMode } from "@/core/threads/agent-mode";

const ATTACHMENT_ACCEPT = [
  "text/*",
  ".md",
  ".markdown",
  ".txt",
  ".py",
  ".js",
  ".ts",
  ".tsx",
  ".jsx",
  ".json",
  ".yaml",
  ".yml",
  ".csv",
  ".tsv",
  ".xml",
  ".html",
  ".css",
  ".sh",
  ".bash",
  ".sql",
  ".toml",
  ".ini",
  ".cfg",
  ".conf",
  ".log",
].join(",");

const THINKING_MODE_ICONS: Record<
  AgentMode,
  ComponentType<{ className?: string }>
> = {
  flash: ZapIcon,
  thinking: LightbulbIcon,
  pro: TargetIcon,
  ultra: RocketIcon,
};

export function skillBuilderAvailableThinkingModes(
  model:
    | Pick<Model, "supports_thinking" | "supports_reasoning_effort">
    | undefined,
): AgentMode[] {
  if (!model?.supports_thinking) return ["flash"];
  if (!model.supports_reasoning_effort) return ["flash", "thinking"];
  return ["flash", "thinking", "pro", "ultra"];
}

export function SkillBuilderComposerAttachments({
  attachments,
  disabled,
  onRemove,
}: {
  attachments: SkillBuilderAttachment[];
  disabled: boolean;
  onRemove: (name: string) => void;
}) {
  const { t } = useI18n();
  if (attachments.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5 px-2 pt-2">
      {attachments.map((item) => (
        <span
          key={item.name}
          className="bg-muted flex max-w-56 items-center gap-1 rounded-lg px-2 py-1 text-xs"
        >
          <FileTextIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="min-w-0 truncate" title={item.name}>
            {item.name}
          </span>
          <button
            type="button"
            aria-label={t.skills.builder.composer.removeAttachment(item.name)}
            className="text-muted-foreground hover:text-foreground shrink-0 disabled:opacity-50"
            disabled={disabled}
            onClick={() => onRemove(item.name)}
          >
            <XIcon aria-hidden className="size-3.5" />
          </button>
        </span>
      ))}
    </div>
  );
}

export function SkillBuilderComposerControls({
  attachDisabled,
  pickersDisabled,
  models,
  selectedModel,
  thinkingMode,
  onPickFiles,
  onSelectModel,
  onSelectThinkingMode,
}: {
  attachDisabled: boolean;
  pickersDisabled: boolean;
  models: Model[];
  selectedModel: Model | undefined;
  thinkingMode: AgentMode;
  onPickFiles: (files: File[]) => void;
  onSelectModel: (name: string) => void;
  onSelectThinkingMode: (mode: AgentMode) => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.composer;
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const thinkingModes = skillBuilderAvailableThinkingModes(selectedModel);
  const ThinkingModeIcon = THINKING_MODE_ICONS[thinkingMode];

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-0.5">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        accept={ATTACHMENT_ACCEPT}
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = "";
          if (files.length > 0) onPickFiles(files);
        }}
      />
      <Button
        type="button"
        size="icon-sm"
        variant="ghost"
        className="text-muted-foreground hover:text-foreground"
        aria-label={copy.addReference}
        disabled={attachDisabled}
        onClick={() => fileInputRef.current?.click()}
      >
        <PaperclipIcon aria-hidden className="size-4" />
      </Button>

      {models.length > 0 ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              className="text-muted-foreground hover:text-foreground h-8 gap-1 px-2 text-xs"
              aria-label={copy.selectModel}
              disabled={pickersDisabled}
            >
              <span className="max-w-36 truncate">
                {selectedModel?.display_name ?? copy.defaultModel}
              </span>
              <ChevronDownIcon aria-hidden className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="start"
            className="max-h-72 overflow-y-auto"
          >
            {models.map((model) => (
              <DropdownMenuItem
                key={model.name}
                onSelect={() => onSelectModel(model.name)}
              >
                <span className="min-w-0 flex-1 truncate">
                  {model.display_name}
                </span>
                {model.is_default ? (
                  <span className="text-muted-foreground text-[10px]">
                    {copy.defaultBadge}
                  </span>
                ) : null}
                {selectedModel?.name === model.name ? (
                  <CheckIcon aria-hidden className="size-3.5" />
                ) : null}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}

      {thinkingModes.length > 1 ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              className="text-muted-foreground hover:text-foreground h-8 gap-1 px-2 text-xs"
              aria-label={copy.selectThinking}
              disabled={pickersDisabled}
            >
              <ThinkingModeIcon aria-hidden className="size-3.5" />
              {copy.mode[thinkingMode]}
              <ChevronDownIcon aria-hidden className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            {thinkingModes.map((mode) => {
              const ModeIcon = THINKING_MODE_ICONS[mode];
              return (
                <DropdownMenuItem
                  key={mode}
                  onSelect={() => onSelectThinkingMode(mode)}
                >
                  <ModeIcon aria-hidden className="size-4" />
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm">{copy.mode[mode]}</span>
                    <span className="text-muted-foreground block text-xs">
                      {copy.modeDescription[mode]}
                    </span>
                  </span>
                  {mode === thinkingMode ? (
                    <CheckIcon aria-hidden className="size-3.5" />
                  ) : null}
                </DropdownMenuItem>
              );
            })}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
    </div>
  );
}
