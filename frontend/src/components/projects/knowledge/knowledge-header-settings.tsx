"use client";

import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useI18n } from "@/core/i18n/hooks";
import type {
  KnowledgeChunkPreviewResponse,
  KnowledgeHeaderRule,
} from "@/core/knowledge/types";

type TableSource = KnowledgeChunkPreviewResponse["table_sources"][number];

export function KnowledgeHeaderSettings({
  sources,
  rules,
  disabled,
  onChange,
}: {
  sources: TableSource[];
  rules: KnowledgeHeaderRule[];
  disabled: boolean;
  onChange: (rule: KnowledgeHeaderRule) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge.wizard;

  if (sources.length === 0) return null;

  return (
    <section
      className="border-border/60 space-y-3 border-b px-4 py-3"
      data-testid="knowledge-header-settings"
    >
      <h3 className="text-xs font-semibold">{labels.headerSettingsTitle}</h3>
      {sources.map((source) => {
        const sourceName = source.sheet ?? labels.headerCsvSource;
        const selected = rules.find((rule) => rule.sheet === source.sheet);
        const mode = selected?.mode ?? source.header_mode;
        const row =
          mode === "explicit"
            ? (selected?.row ?? source.header_row ?? 1)
            : null;
        return (
          <div
            key={source.sheet ?? "__csv__"}
            className="bg-muted/35 grid gap-2 rounded-lg p-3"
            data-testid="knowledge-header-source"
          >
            <div className="flex items-center justify-between gap-3">
              <span className="font-medium">{sourceName}</span>
              <Select
                value={mode}
                disabled={disabled}
                onValueChange={(value) => {
                  const nextMode = value as KnowledgeHeaderRule["mode"];
                  onChange({
                    sheet: source.sheet,
                    mode: nextMode,
                    row:
                      nextMode === "explicit"
                        ? (selected?.row ?? source.header_row ?? 1)
                        : null,
                  });
                }}
              >
                <SelectTrigger
                  size="sm"
                  className="h-8 w-36 rounded-lg text-xs"
                  aria-label={labels.headerModeLabel(sourceName)}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">{labels.headerAuto}</SelectItem>
                  <SelectItem value="none">{labels.headerNone}</SelectItem>
                  <SelectItem value="explicit">
                    {labels.headerExplicit}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            {mode === "explicit" ? (
              <Input
                type="number"
                min={1}
                value={row ?? 1}
                disabled={disabled}
                className="h-8 rounded-lg text-xs"
                aria-label={labels.headerRowLabel(sourceName)}
                onChange={(event) => {
                  const nextRow = Number.parseInt(event.target.value, 10);
                  if (Number.isSafeInteger(nextRow) && nextRow > 0) {
                    onChange({
                      sheet: source.sheet,
                      mode: "explicit",
                      row: nextRow,
                    });
                  }
                }}
              />
            ) : null}
            <p className="text-muted-foreground text-xs">
              {mode === "none"
                ? labels.headerNotSelected
                : mode === "explicit"
                  ? labels.headerSelectedRow(row ?? 1)
                  : source.header_row === null
                    ? labels.headerNotSelected
                    : labels.headerCandidateRow(source.header_row)}
            </p>
            {source.header_cells.length > 0 ? (
              <p className="text-xs break-words">
                {source.header_cells.join(" · ")}
              </p>
            ) : null}
          </div>
        );
      })}
    </section>
  );
}
