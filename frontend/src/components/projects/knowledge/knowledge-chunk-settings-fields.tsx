"use client";

import { LayersIcon, WaypointsIcon } from "lucide-react";

import { Input } from "@/components/ui/input";
import { useI18n } from "@/core/i18n/hooks";
import {
  KNOWLEDGE_CHUNK_OVERLAP_MIN,
  type KnowledgeChunkLimits,
} from "@/core/knowledge/chunk-settings";
import type { KnowledgeChunkingMode } from "@/core/knowledge/types";
import { cn } from "@/lib/utils";

/** Form buffer for chunk parameters; numbers stay as typed until validated. */
export type KnowledgeChunkSettingsDraft = {
  chunkSize: string;
  chunkOverlap: string;
  chunkSeparator: string;
  chunkingMode: KnowledgeChunkingMode;
  childChunkSize: string;
  childChunkSeparator: string;
  removeExtraSpaces: boolean;
  removeUrlsEmails: boolean;
};

const INPUT_CLASS_NAME =
  "bg-background border-input/80 h-9 rounded-lg text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15 md:text-[13px]";

/**
 * The chunking-mode cards shared by the upload wizard and a document's chunk
 * settings page: one card per mode, the selected card expands into its
 * parameters and the preprocessing rules. `limits` are the server's Token
 * bounds when known; the inputs otherwise fall back to unbounded attributes
 * and the caller validates against the client mirror.
 */
export function KnowledgeChunkSettingsFields({
  value,
  onChange,
  disabled,
  limits,
  radioName,
}: {
  value: KnowledgeChunkSettingsDraft;
  onChange: (next: KnowledgeChunkSettingsDraft) => void;
  disabled: boolean;
  limits?: KnowledgeChunkLimits;
  radioName: string;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const wizard = labels.wizard;
  const update = (patch: Partial<KnowledgeChunkSettingsDraft>) =>
    onChange({ ...value, ...patch });

  return (
    <fieldset className="grid gap-2.5">
      <legend className="sr-only">{labels.documents.chunkingModeLabel}</legend>
      {(
        [
          [
            "general",
            labels.documents.chunkingModeGeneral,
            labels.documents.chunkingModeGeneralHint,
          ],
          [
            "parent_child",
            labels.documents.chunkingModeParentChild,
            labels.documents.chunkingModeParentChildHint,
          ],
        ] as const
      ).map(([mode, label, hint]) => (
        <div
          key={mode}
          className={cn(
            "overflow-hidden rounded-xl border transition-colors",
            value.chunkingMode === mode
              ? "border-blue-600"
              : "border-border/70",
          )}
        >
          <label className="bg-muted/35 flex min-h-16 cursor-pointer items-start gap-3 p-3">
            <span
              aria-hidden
              className="border-border/50 bg-background flex size-8 shrink-0 items-center justify-center rounded-lg border shadow-xs"
            >
              {mode === "general" ? (
                <LayersIcon className="size-4 text-blue-600" />
              ) : (
                <WaypointsIcon className="size-4 text-sky-500" />
              )}
            </span>
            <span className="min-w-0 flex-1 space-y-0.5">
              <span className="block text-[13px] font-semibold">{label}</span>
              <span className="text-muted-foreground block text-xs leading-5">
                {hint}
              </span>
            </span>
            <input
              type="radio"
              name={radioName}
              value={mode}
              className="mt-1 size-4 shrink-0 accent-blue-600"
              checked={value.chunkingMode === mode}
              disabled={disabled}
              onChange={() => update({ chunkingMode: mode })}
            />
          </label>
          {value.chunkingMode === mode ? (
            <div className="bg-background space-y-4 p-4">
              {mode === "parent_child" ? (
                <h3 className="text-xs font-semibold">
                  {wizard.parentContextTitle}
                </h3>
              ) : null}
              <div className="grid items-start gap-3 sm:grid-cols-2 xl:grid-cols-3">
                <label className="grid gap-1.5 text-[13px]">
                  <span className="font-medium">
                    {labels.documents.chunkSeparatorLabel}
                  </span>
                  <Input
                    className={INPUT_CLASS_NAME}
                    required
                    maxLength={64}
                    disabled={disabled}
                    value={value.chunkSeparator}
                    onChange={(event) =>
                      update({ chunkSeparator: event.target.value })
                    }
                  />
                </label>
                <label className="grid gap-1.5 text-[13px]">
                  <span className="font-medium">
                    {wizard.chunkSizeTokenLabel}
                  </span>
                  <Input
                    className={INPUT_CLASS_NAME}
                    type="number"
                    min={limits?.parent_min}
                    max={limits?.parent_max}
                    required
                    disabled={disabled}
                    value={value.chunkSize}
                    onChange={(event) =>
                      update({ chunkSize: event.target.value })
                    }
                  />
                </label>
                <label className="grid gap-1.5 text-[13px]">
                  <span className="font-medium">
                    {wizard.chunkOverlapTokenLabel}
                  </span>
                  <Input
                    className={INPUT_CLASS_NAME}
                    type="number"
                    min={KNOWLEDGE_CHUNK_OVERLAP_MIN}
                    max={limits?.overlap_max}
                    required
                    disabled={disabled}
                    value={value.chunkOverlap}
                    onChange={(event) =>
                      update({ chunkOverlap: event.target.value })
                    }
                  />
                </label>
              </div>
              {mode === "parent_child" ? (
                <div className="space-y-2">
                  <h3 className="text-xs font-semibold">
                    {wizard.childRetrievalTitle}
                  </h3>
                  <div className="grid grid-cols-2 items-start gap-3">
                    <label className="grid gap-1.5 text-[13px]">
                      <span className="font-medium">
                        {wizard.childChunkSizeTokenLabel}
                      </span>
                      <Input
                        className={INPUT_CLASS_NAME}
                        type="number"
                        min={limits?.child_min}
                        max={limits?.child_max}
                        required
                        disabled={disabled}
                        value={value.childChunkSize}
                        onChange={(event) =>
                          update({ childChunkSize: event.target.value })
                        }
                      />
                    </label>
                    <label className="grid gap-1.5 text-[13px]">
                      <span className="font-medium">
                        {labels.documents.childChunkSeparatorLabel}
                      </span>
                      <Input
                        className={INPUT_CLASS_NAME}
                        required
                        maxLength={64}
                        disabled={disabled}
                        value={value.childChunkSeparator}
                        onChange={(event) =>
                          update({ childChunkSeparator: event.target.value })
                        }
                      />
                    </label>
                  </div>
                </div>
              ) : null}
              <fieldset className="border-border/60 grid gap-2 border-t pt-3 text-[13px]">
                <legend className="bg-background pr-2 text-xs font-semibold">
                  {labels.documents.preprocessingLabel}
                </legend>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    className="size-4 accent-blue-600"
                    checked={value.removeExtraSpaces}
                    disabled={disabled}
                    onChange={(event) =>
                      update({ removeExtraSpaces: event.target.checked })
                    }
                  />
                  {labels.documents.removeExtraSpacesLabel}
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    className="size-4 accent-blue-600"
                    checked={value.removeUrlsEmails}
                    disabled={disabled}
                    onChange={(event) =>
                      update({ removeUrlsEmails: event.target.checked })
                    }
                  />
                  {labels.documents.removeUrlsEmailsLabel}
                </label>
              </fieldset>
            </div>
          ) : null}
        </div>
      ))}
    </fieldset>
  );
}
