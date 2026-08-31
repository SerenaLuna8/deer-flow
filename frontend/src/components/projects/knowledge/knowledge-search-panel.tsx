"use client";

import {
  FileTextIcon,
  HistoryIcon,
  InfoIcon,
  PlusIcon,
  SearchIcon,
  SlidersHorizontalIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { isKnowledgeConflictError } from "@/core/knowledge/api";
import {
  useKnowledgeBaseQueries,
  useKnowledgeMetadataFields,
  useKnowledgeSearch,
  useKnowledgeSearchHitDetail,
} from "@/core/knowledge/hooks";
import { formatKnowledgeSourcePosition } from "@/core/knowledge/source-position";
import type {
  KnowledgeBaseItem,
  KnowledgeHitDiagnostics,
  KnowledgeMetadataFieldItem,
  KnowledgeMetadataFilterInput,
  KnowledgeMetadataFilterOperator,
  KnowledgeRetrievalMode,
  KnowledgeSearchCitation,
  KnowledgeSearchDiagnostics,
  KnowledgeSearchInput,
  KnowledgeSearchResponse,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";
import { cn } from "@/lib/utils";

import { knowledgeErrorMessage } from "./knowledge-error";

/** Matches the backend cap on metadata_filters per search. */
const MAX_METADATA_FILTERS = 10;

type FilterDraft = {
  key: number;
  name: string;
  operator: KnowledgeMetadataFilterOperator;
  value: string;
};

function operatorsForField(
  field: KnowledgeMetadataFieldItem | undefined,
): KnowledgeMetadataFilterOperator[] {
  if (field === undefined || field.field_type === "string") {
    return ["eq", "contains"];
  }
  return ["eq", "gte", "lte"];
}

/** Converts one draft's value text to the API value; undefined is invalid. */
function filterDraftValue(
  field: KnowledgeMetadataFieldItem | undefined,
  text: string,
): string | number | undefined {
  if (field === undefined || text.trim() === "") return undefined;
  if (field.field_type === "string") return text;
  if (field.field_type === "number") {
    const parsed = Number.parseFloat(text);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  // datetime-local values parse as local time; the API takes epoch seconds.
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? Math.round(ms / 1000) : undefined;
}

/** Retrieval test scoped to one knowledge base, like Dify's in-base 召回测试. */
export function KnowledgeSearchPanel({
  scope,
  base,
  onLocateSegment,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  /** Jumps to the documents view pinned to this segment. */
  onLocateSegment?: (documentId: string, segmentId: string) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const search = useKnowledgeSearch(scope);
  const metadataFields = useKnowledgeMetadataFields(scope, base.id);
  const [query, setQuery] = useState("");
  // Empty inputs defer to the base defaults resolved server-side.
  const [topK, setTopK] = useState("");
  const [threshold, setThreshold] = useState("");
  const [retrievalMode, setRetrievalMode] = useState<
    "default" | KnowledgeRetrievalMode
  >("default");
  const [filters, setFilters] = useState<FilterDraft[]>([]);
  const [nextFilterKey, setNextFilterKey] = useState(1);
  /** Last submitted input, kept so an error can be retried verbatim. */
  const [lastInput, setLastInput] = useState<KnowledgeSearchInput | null>(null);
  const [openHit, setOpenHit] = useState<KnowledgeSearchCitation | null>(null);

  // Any change to what the scores mean — reranker or embedding rebind, mode
  // or default changes, rebuild/reparse generations — must not leave stale
  // results (or an open detail) next to the new configuration.
  const baseConfigKey = [
    base.embedding_model_id,
    base.reranker_model_id ?? "",
    base.retrieval_mode,
    base.default_top_k,
    base.default_score_threshold,
    base.updated_at,
  ].join("|");
  const resetSearch = search.reset;
  useEffect(() => {
    resetSearch();
    setLastInput(null);
    setOpenHit(null);
  }, [baseConfigKey, resetSearch]);

  const parsedTopK = topK.trim() === "" ? undefined : Number.parseInt(topK, 10);
  const topKValid =
    parsedTopK === undefined ||
    (Number.isSafeInteger(parsedTopK) && parsedTopK >= 1 && parsedTopK <= 20);
  const parsedThreshold =
    threshold.trim() === "" ? undefined : Number.parseFloat(threshold);
  const thresholdValid =
    parsedThreshold === undefined ||
    (Number.isFinite(parsedThreshold) &&
      parsedThreshold >= 0 &&
      parsedThreshold <= 1);

  const fieldItems = metadataFields.data ?? [];
  const fieldByName = new Map(fieldItems.map((field) => [field.name, field]));
  const filterInputs: KnowledgeMetadataFilterInput[] = [];
  let filtersValid = true;
  for (const draft of filters) {
    const value = filterDraftValue(fieldByName.get(draft.name), draft.value);
    if (value === undefined) {
      filtersValid = false;
      break;
    }
    filterInputs.push({
      name: draft.name,
      operator: draft.operator,
      value,
    });
  }

  const addFilter = () => {
    const first = fieldItems[0];
    if (!first || filters.length >= MAX_METADATA_FILTERS) return;
    setFilters((current) => [
      ...current,
      { key: nextFilterKey, name: first.name, operator: "eq", value: "" },
    ]);
    setNextFilterKey((key) => key + 1);
  };

  const updateFilter = (key: number, patch: Partial<FilterDraft>) => {
    setFilters((current) =>
      current.map((draft) =>
        draft.key === key ? { ...draft, ...patch } : draft,
      ),
    );
  };

  return (
    <section aria-label={labels.search.title} className="space-y-5 text-[13px]">
      <div>
        <h2 className="text-base font-semibold tracking-tight">
          {labels.search.title}
        </h2>
        <p className="text-muted-foreground mt-1 text-xs leading-5">
          {labels.search.workspaceHint}
        </p>
      </div>
      <div className="border-border/80 bg-background grid overflow-hidden rounded-xl border xl:grid-cols-[320px_minmax(0,1fr)] 2xl:grid-cols-[360px_minmax(0,1fr)]">
        <form
          className="border-border/70 flex min-w-0 flex-col gap-5 border-b p-5 xl:border-r xl:border-b-0"
          onSubmit={(event) => {
            event.preventDefault();
            if (!query.trim() || !topKValid || !thresholdValid || !filtersValid)
              return;
            // The retrieval test always asks for the bounded safe diagnostics;
            // they exist only in this response and are never logged.
            const input: KnowledgeSearchInput = {
              query: query.trim(),
              knowledge_base_ids: [base.id],
              debug: true,
            };
            if (parsedTopK !== undefined) {
              input.top_k = parsedTopK;
            }
            if (parsedThreshold !== undefined) {
              input.score_threshold = parsedThreshold;
            }
            if (retrievalMode !== "default") {
              input.retrieval_mode = retrievalMode;
            }
            if (filterInputs.length > 0) {
              input.metadata_filters = filterInputs;
            }
            setLastInput(input);
            setOpenHit(null);
            search.mutate(input);
          }}
        >
          <label className="grid gap-2 text-[13px]">
            <span className="flex items-center gap-2 font-semibold">
              <SearchIcon
                aria-hidden
                className="text-muted-foreground size-4"
              />
              {labels.search.queryLabel}
            </span>
            <Input
              className="border-input/80 bg-background h-10 rounded-lg text-[13px] shadow-none md:text-[13px]"
              value={query}
              required
              maxLength={2000}
              placeholder={labels.search.queryPlaceholder}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <div className="border-border/60 space-y-4 border-t pt-4">
            <h3 className="flex items-center gap-2 text-[13px] font-semibold">
              <SlidersHorizontalIcon
                aria-hidden
                className="text-muted-foreground size-4"
              />
              {labels.search.parametersTitle}
            </h3>
            <div className="grid grid-cols-2 gap-3">
              <label className="grid gap-1.5 text-[13px]">
                <span className="font-medium">{labels.search.topKLabel}</span>
                <Input
                  className="border-border/70 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                  type="number"
                  min={1}
                  max={20}
                  value={topK}
                  placeholder={String(base.default_top_k)}
                  onChange={(event) => setTopK(event.target.value)}
                />
                <span className="text-muted-foreground text-xs">
                  {labels.search.topKHint}
                </span>
              </label>
              <label className="grid gap-1.5 text-[13px]">
                <span className="font-medium">
                  {labels.search.thresholdLabel}
                </span>
                <Input
                  className="border-border/70 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={threshold}
                  placeholder={String(base.default_score_threshold)}
                  onChange={(event) => setThreshold(event.target.value)}
                />
                <span className="text-muted-foreground text-xs">
                  {labels.search.thresholdHint}
                </span>
              </label>
            </div>
            <label className="grid gap-1.5 text-[13px]">
              <span className="font-medium">
                {labels.search.retrievalModeLabel}
              </span>
              <Select
                value={retrievalMode}
                onValueChange={(value) =>
                  setRetrievalMode(value as "default" | KnowledgeRetrievalMode)
                }
              >
                <SelectTrigger
                  className="border-input/80 bg-background w-full rounded-lg text-[13px] shadow-none"
                  aria-label={labels.search.retrievalModeLabel}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="rounded-lg [&_[data-slot=select-item]]:text-[13px]">
                  <SelectItem value="default">
                    {labels.search.retrievalModes.default}
                  </SelectItem>
                  <SelectItem value="semantic">
                    {labels.search.retrievalModes.semantic}
                  </SelectItem>
                  <SelectItem value="hybrid">
                    {labels.search.retrievalModes.hybrid}
                  </SelectItem>
                </SelectContent>
              </Select>
            </label>
          </div>
          <fieldset className="border-border/60 grid gap-2 border-t pt-3 text-[13px]">
            <legend className="pr-2 font-medium">
              {labels.search.filtersLabel}
            </legend>
            {filters.length > 0 ? (
              <p className="text-muted-foreground text-xs">
                {labels.search.filtersHint}
              </p>
            ) : null}
            {filters.map((draft, index) => {
              const field = fieldByName.get(draft.name);
              const operators = operatorsForField(field);
              const valueType =
                field?.field_type === "number"
                  ? "number"
                  : field?.field_type === "time"
                    ? "datetime-local"
                    : "text";
              return (
                <div
                  key={draft.key}
                  className="flex flex-wrap items-center gap-2"
                  data-testid="knowledge-filter-row"
                >
                  <Select
                    value={draft.name}
                    onValueChange={(name) => {
                      // The value input type changes with the field type, so a
                      // stale value text would be misleading; operators reset
                      // to the always-valid eq.
                      updateFilter(draft.key, {
                        name,
                        operator: "eq",
                        value: "",
                      });
                    }}
                  >
                    <SelectTrigger
                      className="border-border/70 bg-background w-40 rounded-lg text-[13px] shadow-none"
                      aria-label={labels.search.filterFieldAria(index + 1)}
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="rounded-lg [&_[data-slot=select-item]]:text-[13px]">
                      {fieldItems.map((item) => (
                        <SelectItem key={item.id} value={item.name}>
                          {item.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Select
                    value={draft.operator}
                    onValueChange={(operator) =>
                      updateFilter(draft.key, {
                        operator: operator as KnowledgeMetadataFilterOperator,
                      })
                    }
                  >
                    <SelectTrigger
                      className="border-border/70 bg-background w-28 rounded-lg text-[13px] shadow-none"
                      aria-label={labels.search.filterOperatorAria(index + 1)}
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="rounded-lg [&_[data-slot=select-item]]:text-[13px]">
                      {operators.map((operator) => (
                        <SelectItem key={operator} value={operator}>
                          {labels.search.operators[operator]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Input
                    className="border-border/70 bg-background h-9 w-44 flex-1 rounded-lg text-[13px] shadow-none md:text-[13px]"
                    type={valueType}
                    step={valueType === "number" ? "any" : undefined}
                    value={draft.value}
                    placeholder={labels.search.filterValuePlaceholder}
                    aria-label={labels.search.filterValueAria(index + 1)}
                    onChange={(event) =>
                      updateFilter(draft.key, { value: event.target.value })
                    }
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="text-muted-foreground size-8 rounded-lg"
                    aria-label={labels.search.removeFilterAria(index + 1)}
                    onClick={() =>
                      setFilters((current) =>
                        current.filter((item) => item.key !== draft.key),
                      )
                    }
                  >
                    <XIcon aria-hidden className="size-4" />
                  </Button>
                </div>
              );
            })}
            {fieldItems.length === 0 ? (
              filters.length === 0 ? (
                <p className="text-muted-foreground text-xs">
                  {labels.search.filterNoFields}
                </p>
              ) : null
            ) : (
              <div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="border-border/70 rounded-lg text-[13px] shadow-none"
                  disabled={filters.length >= MAX_METADATA_FILTERS}
                  onClick={addFilter}
                >
                  <PlusIcon aria-hidden className="size-4" />
                  {labels.search.addFilter}
                </Button>
              </div>
            )}
          </fieldset>
          <div className="pt-1">
            <Button
              type="submit"
              className="h-9 w-full rounded-lg text-[13px] shadow-none"
              disabled={
                search.isPending ||
                !query.trim() ||
                !topKValid ||
                !thresholdValid ||
                !filtersValid
              }
            >
              <SearchIcon aria-hidden className="size-4" />
              {search.isPending
                ? labels.search.searching
                : labels.search.submit}
            </Button>
          </div>
        </form>

        <div
          className="bg-muted/15 flex min-w-0 flex-col p-5"
          data-testid="knowledge-search-outcome"
        >
          <div className="border-border/60 mb-4 flex min-h-6 items-center justify-between gap-3 border-b pb-4">
            <h3 className="flex items-center gap-2 text-[13px] font-semibold">
              <FileTextIcon
                aria-hidden
                className="text-muted-foreground size-4"
              />
              {labels.search.outcomeTitle}
            </h3>
            <details
              className="relative shrink-0"
              data-testid="knowledge-score-help"
            >
              <summary
                aria-label={labels.search.scoreHelp}
                className="text-muted-foreground hover:bg-muted focus-visible:ring-selection/40 flex size-6 cursor-pointer list-none items-center justify-center rounded-md focus-visible:ring-2 focus-visible:outline-none [&::-webkit-details-marker]:hidden"
              >
                <InfoIcon aria-hidden className="size-3.5" />
              </summary>
              <div className="border-border/80 bg-popover text-popover-foreground absolute top-8 right-0 z-10 w-72 max-w-[calc(100vw-4rem)] rounded-lg border p-3 text-xs leading-5 shadow-md">
                {labels.search.description}
              </div>
            </details>
          </div>
          {search.error ? (
            <div
              role="alert"
              data-testid="knowledge-search-error"
              className="border-destructive/40 bg-destructive/5 space-y-3 rounded-lg border px-4 py-4"
            >
              <p className="text-destructive text-[13px]">
                {knowledgeErrorMessage(search.error, labels.errors)}
              </p>
              {lastInput ? (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="rounded-lg text-[13px] shadow-none"
                  disabled={search.isPending}
                  onClick={() => search.mutate(lastInput)}
                >
                  {labels.search.retry}
                </Button>
              ) : null}
            </div>
          ) : null}

          {search.isPending ? (
            <div className="grid gap-3" aria-hidden>
              <Skeleton className="h-32 rounded-lg" />
              <Skeleton className="h-32 rounded-lg" />
            </div>
          ) : null}

          {!search.isPending && search.error === null && !search.data ? (
            <div
              className="flex min-h-72 flex-1 flex-col items-center justify-center px-5 py-10 text-center"
              data-testid="knowledge-search-never"
            >
              <span className="border-border/70 bg-background text-muted-foreground mb-4 inline-flex size-11 items-center justify-center rounded-xl border">
                <SearchIcon aria-hidden className="size-5" strokeWidth={1.5} />
              </span>
              <p className="text-[13px] font-medium">
                {labels.search.waitingTitle}
              </p>
              <p className="text-muted-foreground mt-2 max-w-64 text-xs leading-5">
                {labels.search.neverSearched}
              </p>
            </div>
          ) : null}

          {search.data && !search.isPending ? (
            <SearchOutcome data={search.data} onOpenHit={setOpenHit} />
          ) : null}
        </div>
      </div>

      {openHit ? (
        <SearchHitDetailDialog
          scope={scope}
          baseId={base.id}
          citation={openHit}
          hitDiagnostics={search.data?.diagnostics?.hit_diagnostics.find(
            (hit) => hit.segment_id === openHit.segment_id,
          )}
          onClose={() => setOpenHit(null)}
          onLocate={
            onLocateSegment
              ? (documentId, segmentId) => {
                  setOpenHit(null);
                  onLocateSegment(documentId, segmentId);
                }
              : undefined
          }
        />
      ) : null}

      <RecentQueriesSection scope={scope} base={base} onPick={setQuery} />
    </section>
  );
}

/** Results, per-hit provenance, and the collapsed safe diagnostics. */
function SearchOutcome({
  data,
  onOpenHit,
}: {
  data: KnowledgeSearchResponse;
  onOpenHit: (citation: KnowledgeSearchCitation) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const diagnostics = data.diagnostics;
  const hitBySegment = new Map(
    (diagnostics?.hit_diagnostics ?? []).map((hit) => [hit.segment_id, hit]),
  );

  if (data.citations.length === 0) {
    const reason = diagnostics?.empty_reason ?? null;
    return (
      <p
        className="text-muted-foreground flex min-h-72 items-center justify-center px-5 py-10 text-center text-[13px] leading-5"
        data-testid="knowledge-search-empty"
      >
        {reason ? labels.search.emptyReasons[reason] : labels.search.empty}
      </p>
    );
  }

  return (
    <section
      aria-label={labels.search.resultsTitle(data.citations.length)}
      className="space-y-3"
    >
      <h3 className="text-[13px] font-semibold">
        {labels.search.resultsTitle(data.citations.length)}
      </h3>
      <ol className="grid gap-2" data-testid="knowledge-search-results">
        {data.citations.map((citation, index) => {
          const position = formatKnowledgeSourcePosition(
            citation.source_position,
            labels.sourcePosition,
          );
          const hit = hitBySegment.get(citation.segment_id);
          return (
            <li
              key={citation.segment_id}
              className="border-border/70 bg-background overflow-hidden rounded-lg border p-4"
            >
              <div className="border-border/60 bg-muted/25 -mx-4 -mt-4 flex min-w-0 flex-wrap items-center gap-2 border-b px-4 py-2.5">
                <span className="text-muted-foreground bg-muted inline-flex h-5 min-w-5 items-center justify-center rounded px-1 text-xs font-medium tabular-nums">
                  #{index + 1}
                </span>
                <span className="text-foreground truncate text-[13px] font-medium">
                  {citation.document_name}
                </span>
                <span className="text-muted-foreground truncate text-xs">
                  {citation.knowledge_base_name}
                </span>
                <Badge
                  variant="secondary"
                  className="bg-selection-subtle text-selection ml-auto shrink-0 rounded-md font-medium tabular-nums"
                >
                  {labels.search.score(citation.score)}
                </Badge>
                <Badge
                  variant="outline"
                  className="border-border/70 text-muted-foreground shrink-0 rounded-md font-normal"
                >
                  {labels.search.scoreKinds[citation.score_kind ?? "unknown"]}
                </Badge>
              </div>
              <div className="text-muted-foreground mt-2.5 flex flex-wrap items-center gap-x-2 text-xs">
                <span>
                  {labels.citations.segmentPosition(citation.segment_position)}
                </span>
                {position ? <span>· {position}</span> : null}
              </div>
              <p className="text-foreground/85 mt-2 line-clamp-4 text-[13px] leading-6 [overflow-wrap:anywhere] whitespace-normal">
                {citation.snippet}
              </p>
              <div className="mt-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="border-border/70 rounded-lg text-[13px] shadow-none"
                  onClick={() => onOpenHit(citation)}
                >
                  {labels.search.openDetail(citation.segment_position)}
                </Button>
              </div>
              {hit ? (
                <details className="border-border/50 mt-3 border-t pt-2 text-xs">
                  <summary className="text-muted-foreground cursor-pointer">
                    {labels.search.hitDiagnosticsSummary}
                  </summary>
                  <div className="text-muted-foreground mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
                    <span>
                      {labels.search.localScore(hit.local_score)} ·{" "}
                      {labels.search.scoreKinds[hit.local_score_kind]}
                    </span>
                    <span>
                      {labels.search.rankingScore(hit.ranking_score)} ·{" "}
                      {labels.search.scoreKinds[hit.ranking_method]}
                    </span>
                    {hit.matched_children.map((child) => (
                      <span key={child.child_id} className="tabular-nums">
                        C-{child.position} ·{" "}
                        {labels.search.detail.routes[child.route]} ·{" "}
                        {child.score.toFixed(3)}
                      </span>
                    ))}
                  </div>
                </details>
              ) : null}
            </li>
          );
        })}
      </ol>
      {diagnostics ? (
        <SearchDiagnosticsDisclosure diagnostics={diagnostics} />
      ) : null}
    </section>
  );
}

/** The bounded safe diagnostics for this one response, collapsed by default. */
function SearchDiagnosticsDisclosure({
  diagnostics,
}: {
  diagnostics: KnowledgeSearchDiagnostics;
}) {
  const { t } = useI18n();
  const labels = t.knowledge.search.diagnostics;
  const rows: Array<[string, string]> = [
    [labels.strategyVersion, diagnostics.strategy_version],
    [labels.retrievalMode, diagnostics.retrieval_mode],
    [labels.targetBases, String(diagnostics.target_base_count)],
    [labels.routeBudget, String(diagnostics.per_base_route_budget)],
    [labels.models, diagnostics.model_ids.join(", ") || "—"],
    [labels.semanticCandidates, String(diagnostics.counts.semantic_candidates)],
    [labels.lexicalCandidates, String(diagnostics.counts.lexical_candidates)],
    [
      labels.parentsDeduplicated,
      String(diagnostics.counts.parents_deduplicated),
    ],
    [labels.thresholdFiltered, String(diagnostics.counts.threshold_filtered)],
    [labels.staleFiltered, String(diagnostics.counts.stale_filtered)],
    [labels.returned, String(diagnostics.counts.returned)],
    [
      labels.embeddingMs,
      `${diagnostics.timings.query_embedding_ms.toFixed(0)} ms`,
    ],
    [labels.recallMs, `${diagnostics.timings.recall_ms.toFixed(0)} ms`],
    [labels.rerankMs, `${diagnostics.timings.rerank_ms.toFixed(0)} ms`],
    [
      labels.finalValidationMs,
      `${diagnostics.timings.final_validation_ms.toFixed(0)} ms`,
    ],
  ];
  return (
    <details
      className="border-border/70 bg-muted/15 rounded-lg border px-4 py-3"
      data-testid="knowledge-search-diagnostics"
    >
      <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-[13px] font-medium transition-colors">
        {labels.title}
      </summary>
      {diagnostics.heterogeneous_without_lexical_evidence ? (
        <p
          role="note"
          className="mt-2 rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-300/30 dark:bg-amber-950/40 dark:text-amber-200"
        >
          {labels.heterogeneousWarning}
        </p>
      ) : null}
      <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
        {rows.map(([term, value]) => (
          <div key={term} className="flex items-baseline justify-between gap-3">
            <dt className="text-muted-foreground">{term}</dt>
            <dd className="truncate text-right font-mono">{value}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

/** Mirrors the backend's child page cap on the segment detail endpoint. */
const SEGMENT_CHILD_PAGE_SIZE = 50;

/**
 * Full original segment for one hit, pinned to the version/digest the score
 * was computed for. A conflict means the document moved on — the dialog asks
 * for a fresh search instead of showing new text under an old score. Matched
 * children are highlighted strictly by the identities this search returned.
 */
function SearchHitDetailDialog({
  scope,
  baseId,
  citation,
  hitDiagnostics,
  onClose,
  onLocate,
}: {
  scope: ProjectClientScope;
  baseId: string;
  citation: KnowledgeSearchCitation;
  hitDiagnostics: KnowledgeHitDiagnostics | undefined;
  onClose: () => void;
  onLocate?: (documentId: string, segmentId: string) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const detailLabels = labels.search.detail;
  const [childPage, setChildPage] = useState(1);
  const detail = useKnowledgeSearchHitDetail(scope, {
    baseId,
    documentId: citation.document_id,
    segmentId: citation.segment_id,
    expectedDocumentVersion: citation.document_version,
    expectedContentDigest: citation.content_digest,
    childPage,
  });
  const conflict =
    detail.error !== null && isKnowledgeConflictError(detail.error);
  const matchedById = new Map(
    (hitDiagnostics?.matched_children ?? []).map((child) => [
      child.child_id,
      child,
    ]),
  );
  const data = detail.data;
  const childPageCount = data
    ? Math.max(1, Math.ceil(data.children_total / SEGMENT_CHILD_PAGE_SIZE))
    : 1;
  const sourcePosition = data
    ? formatKnowledgeSourcePosition(
        data.segment.source_position,
        labels.sourcePosition,
      )
    : null;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="border-border/70 flex max-h-[85vh] flex-col gap-4 overflow-hidden rounded-lg text-[13px] sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="text-base tracking-tight">
            {detailLabels.title}
          </DialogTitle>
          <DialogDescription className="truncate text-xs leading-5">
            {citation.document_name} ·{" "}
            {labels.citations.segmentPosition(citation.segment_position)}
          </DialogDescription>
        </DialogHeader>
        <div
          className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1"
          aria-busy={detail.isLoading}
          data-testid="knowledge-hit-detail"
        >
          {conflict ? (
            <p
              role="alert"
              data-testid="knowledge-detail-conflict"
              className="text-destructive text-[13px]"
            >
              {detailLabels.conflict}
            </p>
          ) : detail.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(detail.error, labels.errors)}
            </p>
          ) : data === undefined ? (
            <Skeleton className="h-40 rounded-lg" />
          ) : (
            <>
              {data.content_state === "stale" ? (
                <p
                  role="alert"
                  className="rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-300/30 dark:bg-amber-950/40 dark:text-amber-200"
                >
                  {detailLabels.staleContent}
                </p>
              ) : null}
              <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                {!data.segment.enabled ? (
                  <Badge variant="outline">{detailLabels.disabledBadge}</Badge>
                ) : null}
                <span>
                  {labels.segments.wordCount(data.segment.word_count)}
                </span>
                {sourcePosition ? <span>· {sourcePosition}</span> : null}
              </div>
              <p
                className="border-border/60 bg-muted/15 rounded-lg border px-3 py-2.5 text-[13px] leading-6 [overflow-wrap:anywhere] break-words whitespace-pre-wrap"
                data-testid="knowledge-detail-content"
              >
                {data.segment.content}
              </p>
              {hitDiagnostics && hitDiagnostics.matched_children.length > 0 ? (
                <section
                  aria-label={detailLabels.matchedChildrenTitle}
                  className="space-y-1.5"
                >
                  <h4 className="text-xs font-semibold">
                    {detailLabels.matchedChildrenTitle}
                  </h4>
                  <ul className="text-muted-foreground grid gap-1 text-xs">
                    {hitDiagnostics.matched_children.map((child) => (
                      <li key={child.child_id} className="tabular-nums">
                        C-{child.position} · {detailLabels.routes[child.route]}{" "}
                        · {child.score.toFixed(3)}
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
              {data.children_total > 0 ? (
                <section
                  aria-label={detailLabels.childrenTitle(data.children_total)}
                  className="space-y-1.5"
                >
                  <h4 className="text-xs font-semibold">
                    {detailLabels.childrenTitle(data.children_total)}
                  </h4>
                  <ol
                    className="grid gap-1.5"
                    data-testid="knowledge-detail-children"
                  >
                    {data.children.map((child) => {
                      const matched = matchedById.get(child.id);
                      return (
                        <li
                          key={child.id}
                          className={cn(
                            "border-border/70 bg-background rounded-lg border px-3 py-2",
                            matched &&
                              "border-selection bg-selection-subtle/40",
                          )}
                        >
                          <p className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs font-medium tabular-nums">
                            C-{child.position}
                            {matched ? (
                              <Badge
                                variant="secondary"
                                className="bg-selection-subtle text-selection rounded-md text-xs font-medium"
                              >
                                {detailLabels.matchedBadge} ·{" "}
                                {detailLabels.routes[matched.route]} ·{" "}
                                {matched.score.toFixed(3)}
                              </Badge>
                            ) : null}
                          </p>
                          <p className="mt-1.5 text-[13px] leading-6 [overflow-wrap:anywhere] break-words whitespace-pre-wrap">
                            {child.content}
                          </p>
                        </li>
                      );
                    })}
                  </ol>
                  {childPageCount > 1 ? (
                    <div className="flex items-center justify-between gap-2 pt-1 text-xs">
                      <span className="text-muted-foreground tabular-nums">
                        {labels.segments.pageInfo(
                          data.child_page,
                          childPageCount,
                          data.children_total,
                        )}
                      </span>
                      <div className="flex items-center gap-1.5">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="border-border/70 rounded-lg text-[13px] shadow-none"
                          disabled={childPage <= 1}
                          onClick={() => setChildPage((page) => page - 1)}
                        >
                          {labels.segments.previousPage}
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="border-border/70 rounded-lg text-[13px] shadow-none"
                          disabled={childPage >= childPageCount}
                          onClick={() => setChildPage((page) => page + 1)}
                        >
                          {labels.segments.nextPage}
                        </Button>
                      </div>
                    </div>
                  ) : null}
                </section>
              ) : null}
            </>
          )}
        </div>
        <DialogFooter className="border-border/60 gap-2 border-t pt-4 sm:justify-between">
          {onLocate ? (
            <Button
              type="button"
              variant="outline"
              className="border-border/70 rounded-lg text-[13px] shadow-none"
              onClick={() =>
                onLocate(citation.document_id, citation.segment_id)
              }
            >
              {detailLabels.locate}
            </Button>
          ) : (
            <span aria-hidden />
          )}
          <Button
            type="button"
            variant="ghost"
            className="rounded-lg text-[13px]"
            onClick={onClose}
          >
            {labels.segments.close}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Query log for this base: retrieval tests and agent calls, newest first. */
function RecentQueriesSection({
  scope,
  base,
  onPick,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  /** Clicking a logged query backfills it into the search input. */
  onPick: (query: string) => void;
}) {
  const { t, locale } = useI18n();
  const labels = t.knowledge;
  const [page, setPage] = useState(1);
  const recent = useKnowledgeBaseQueries(scope, base.id, page);

  const total = recent.data?.total ?? 0;
  const pageSize = recent.data?.page_size ?? 1;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return (
    <section
      aria-label={labels.search.recentTitle}
      className="border-border/80 bg-background overflow-hidden rounded-xl border"
    >
      <h3 className="border-border/60 flex items-center gap-2 border-b px-5 py-4 text-[13px] font-semibold">
        <HistoryIcon aria-hidden className="text-muted-foreground size-4" />
        {labels.search.recentTitle}
      </h3>
      {recent.isLoading ? (
        <Skeleton className="m-5 h-24 rounded-lg" />
      ) : recent.error ? (
        <p role="alert" className="text-destructive p-5 text-[13px]">
          {knowledgeErrorMessage(recent.error, labels.errors)}
        </p>
      ) : (recent.data?.items.length ?? 0) === 0 ? (
        <p className="text-muted-foreground px-5 py-6 text-center text-xs">
          {labels.search.recentEmpty}
        </p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[13px]">
              <thead className="bg-muted/30 text-muted-foreground text-xs [&_th]:font-medium">
                <tr>
                  <th className="px-4 py-2.5">
                    {labels.search.recentColumns.query}
                  </th>
                  <th className="px-4 py-2.5">
                    {labels.search.recentColumns.source}
                  </th>
                  <th className="px-4 py-2.5">
                    {labels.search.recentColumns.results}
                  </th>
                  <th className="px-4 py-2.5">
                    {labels.search.recentColumns.topScore}
                  </th>
                  <th className="px-4 py-2.5">
                    {labels.search.recentColumns.time}
                  </th>
                </tr>
              </thead>
              <tbody data-testid="knowledge-recent-queries">
                {recent.data?.items.map((item) => (
                  <tr
                    key={item.id}
                    className="border-border/60 hover:bg-muted/20 border-t align-top transition-colors"
                  >
                    <td className="max-w-72 px-4 py-2.5">
                      <button
                        type="button"
                        className="hover:text-selection focus-visible:ring-selection/40 block w-full cursor-pointer truncate rounded-sm text-left underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:outline-none"
                        title={item.query}
                        onClick={() => onPick(item.query)}
                      >
                        {item.query}
                      </button>
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge
                        variant="outline"
                        className="border-border/70 text-muted-foreground rounded-md font-normal"
                      >
                        {labels.search.recentSource[item.source]}
                      </Badge>
                    </td>
                    <td className="text-muted-foreground px-4 py-2.5 tabular-nums">
                      {item.result_count}
                    </td>
                    <td className="text-muted-foreground px-4 py-2.5 tabular-nums">
                      {item.top_score === null
                        ? "—"
                        : item.top_score.toFixed(3)}
                    </td>
                    <td className="text-muted-foreground px-4 py-2.5 text-xs whitespace-nowrap">
                      {new Date(item.created_at).toLocaleString(locale)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pageCount > 1 ? (
            <div className="border-border/60 flex items-center justify-between gap-2 border-t px-5 py-3 text-xs">
              <span className="text-muted-foreground tabular-nums">
                {labels.segments.pageInfo(page, pageCount, total)}
              </span>
              <div className="flex items-center gap-1.5">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="border-border/70 rounded-lg text-[13px] shadow-none"
                  disabled={page <= 1}
                  onClick={() => setPage((current) => current - 1)}
                >
                  {labels.segments.previousPage}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="border-border/70 rounded-lg text-[13px] shadow-none"
                  disabled={page >= pageCount}
                  onClick={() => setPage((current) => current + 1)}
                >
                  {labels.segments.nextPage}
                </Button>
              </div>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
