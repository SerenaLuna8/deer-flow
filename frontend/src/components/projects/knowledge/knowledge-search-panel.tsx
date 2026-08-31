"use client";

import { PlusIcon, SearchIcon, XIcon } from "lucide-react";
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
    <section aria-label={labels.search.title} className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">{labels.search.title}</h2>
        <p className="text-muted-foreground mt-1 text-sm">
          {labels.search.description}
        </p>
      </div>
      <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <form
        className="grid gap-4"
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
        <label className="grid gap-1.5 text-sm">
          <span className="font-medium">{labels.search.queryLabel}</span>
          <Input
            value={query}
            required
            maxLength={2000}
            placeholder={labels.search.queryPlaceholder}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{labels.search.topKLabel}</span>
            <Input
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
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{labels.search.thresholdLabel}</span>
            <Input
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
        <label className="grid gap-1.5 text-sm">
          <span className="font-medium">
            {labels.search.retrievalModeLabel}
          </span>
          <Select
            value={retrievalMode}
            onValueChange={(value) =>
              setRetrievalMode(value as "default" | KnowledgeRetrievalMode)
            }
          >
            <SelectTrigger aria-label={labels.search.retrievalModeLabel}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
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
        <fieldset className="grid gap-2 text-sm">
          <legend className="font-medium">{labels.search.filtersLabel}</legend>
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
                    updateFilter(draft.key, { name, operator: "eq", value: "" });
                  }}
                >
                  <SelectTrigger
                    className="w-40"
                    aria-label={labels.search.filterFieldAria(index + 1)}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
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
                    className="w-28"
                    aria-label={labels.search.filterOperatorAria(index + 1)}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {operators.map((operator) => (
                      <SelectItem key={operator} value={operator}>
                        {labels.search.operators[operator]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Input
                  className="w-44 flex-1"
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
                disabled={filters.length >= MAX_METADATA_FILTERS}
                onClick={addFilter}
              >
                <PlusIcon aria-hidden className="size-4" />
                {labels.search.addFilter}
              </Button>
            </div>
          )}
        </fieldset>
        <div>
          <Button
            type="submit"
            disabled={
              search.isPending ||
              !query.trim() ||
              !topKValid ||
              !thresholdValid ||
              !filtersValid
            }
          >
            <SearchIcon aria-hidden className="size-4" />
            {search.isPending ? labels.search.searching : labels.search.submit}
          </Button>
        </div>
      </form>

      <div
        className="min-w-0 space-y-3"
        data-testid="knowledge-search-outcome"
      >
        {search.error ? (
          <div
            role="alert"
            data-testid="knowledge-search-error"
            className="border-destructive/40 bg-destructive/5 space-y-3 rounded-xl border px-4 py-4"
          >
            <p className="text-destructive text-sm">
              {knowledgeErrorMessage(search.error, labels.errors)}
            </p>
            {lastInput ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={search.isPending}
                onClick={() => search.mutate(lastInput)}
              >
                {labels.search.retry}
              </Button>
            ) : null}
          </div>
        ) : null}

        {search.isPending ? (
          <Skeleton aria-hidden className="h-40 rounded-xl" />
        ) : null}

        {!search.isPending && search.error === null && !search.data ? (
          <p
            className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-sm"
            data-testid="knowledge-search-never"
          >
            {labels.search.neverSearched}
          </p>
        ) : null}

        {search.data && !search.isPending ? (
          <SearchOutcome
            data={search.data}
            onOpenHit={setOpenHit}
          />
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
        className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-sm"
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
      <h3 className="text-sm font-semibold">
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
              className="border-border rounded-xl border p-4"
            >
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span className="text-muted-foreground text-xs font-medium tabular-nums">
                  #{index + 1}
                </span>
                <span className="text-foreground truncate text-sm font-medium">
                  {citation.document_name}
                </span>
                <span className="text-muted-foreground truncate text-xs">
                  {citation.knowledge_base_name}
                </span>
                <Badge variant="secondary" className="ml-auto shrink-0">
                  {labels.search.score(citation.score)}
                </Badge>
                <Badge variant="outline" className="shrink-0">
                  {labels.search.scoreKinds[citation.score_kind ?? "unknown"]}
                </Badge>
              </div>
              <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-x-2 text-xs">
                <span>
                  {labels.citations.segmentPosition(citation.segment_position)}
                </span>
                {position ? <span>· {position}</span> : null}
              </div>
              <p className="text-muted-foreground mt-2 text-sm leading-6 whitespace-pre-wrap">
                {citation.snippet}
              </p>
              <div className="mt-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => onOpenHit(citation)}
                >
                  {labels.search.openDetail(citation.segment_position)}
                </Button>
              </div>
              {hit ? (
                <details className="mt-2 text-xs">
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
      {diagnostics ? <SearchDiagnosticsDisclosure diagnostics={diagnostics} /> : null}
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
      className="rounded-xl border px-4 py-3"
      data-testid="knowledge-search-diagnostics"
    >
      <summary className="cursor-pointer text-sm font-medium">
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
      <DialogContent className="flex max-h-[85vh] flex-col gap-4 overflow-hidden sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{detailLabels.title}</DialogTitle>
          <DialogDescription className="truncate">
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
              className="text-destructive text-sm"
            >
              {detailLabels.conflict}
            </p>
          ) : detail.error ? (
            <p role="alert" className="text-destructive text-sm">
              {knowledgeErrorMessage(detail.error, labels.errors)}
            </p>
          ) : data === undefined ? (
            <Skeleton className="h-40 rounded-xl" />
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
                <span>{labels.segments.wordCount(data.segment.word_count)}</span>
                {sourcePosition ? <span>· {sourcePosition}</span> : null}
              </div>
              <p
                className="text-sm leading-6 break-words whitespace-pre-wrap"
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
                        C-{child.position} ·{" "}
                        {detailLabels.routes[child.route]} ·{" "}
                        {child.score.toFixed(3)}
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
                            "rounded-lg border px-3 py-2",
                            matched &&
                              "border-selection bg-selection-subtle/40",
                          )}
                        >
                          <p className="text-muted-foreground flex flex-wrap items-center gap-2 text-[10px] font-medium tabular-nums">
                            C-{child.position}
                            {matched ? (
                              <Badge variant="secondary" className="text-[10px]">
                                {detailLabels.matchedBadge} ·{" "}
                                {detailLabels.routes[matched.route]} ·{" "}
                                {matched.score.toFixed(3)}
                              </Badge>
                            ) : null}
                          </p>
                          <p className="mt-0.5 text-xs break-words whitespace-pre-wrap">
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
                          disabled={childPage <= 1}
                          onClick={() => setChildPage((page) => page - 1)}
                        >
                          {labels.segments.previousPage}
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
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
        <DialogFooter className="gap-2 sm:justify-between">
          {onLocate ? (
            <Button
              type="button"
              variant="outline"
              onClick={() =>
                onLocate(citation.document_id, citation.segment_id)
              }
            >
              {detailLabels.locate}
            </Button>
          ) : (
            <span aria-hidden />
          )}
          <Button type="button" variant="ghost" onClick={onClose}>
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
      className="space-y-3 border-t pt-4"
    >
      <h3 className="text-sm font-semibold">{labels.search.recentTitle}</h3>
      {recent.isLoading ? (
        <Skeleton className="h-24 rounded-xl" />
      ) : recent.error ? (
        <p role="alert" className="text-destructive text-sm">
          {knowledgeErrorMessage(recent.error, labels.errors)}
        </p>
      ) : (recent.data?.items.length ?? 0) === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-6 text-center text-sm">
          {labels.search.recentEmpty}
        </p>
      ) : (
        <>
          <div className="border-border overflow-x-auto rounded-xl border">
            <table className="w-full text-left text-sm">
              <thead className="bg-muted/60">
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
                  <tr key={item.id} className="border-t align-top">
                    <td className="max-w-72 px-4 py-2.5">
                      <button
                        type="button"
                        className="hover:text-foreground focus-visible:ring-ring block w-full cursor-pointer truncate text-left underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:outline-none"
                        title={item.query}
                        onClick={() => onPick(item.query)}
                      >
                        {item.query}
                      </button>
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge variant="outline">
                        {labels.search.recentSource[item.source]}
                      </Badge>
                    </td>
                    <td className="text-muted-foreground px-4 py-2.5 tabular-nums">
                      {item.result_count}
                    </td>
                    <td className="text-muted-foreground px-4 py-2.5 tabular-nums">
                      {item.top_score === null ? "—" : item.top_score.toFixed(3)}
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
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="text-muted-foreground tabular-nums">
                {labels.segments.pageInfo(page, pageCount, total)}
              </span>
              <div className="flex items-center gap-1.5">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage((current) => current - 1)}
                >
                  {labels.segments.previousPage}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
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
