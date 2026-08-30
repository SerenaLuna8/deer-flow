"use client";

import { PlusIcon, SearchIcon, XIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import {
  useKnowledgeBaseQueries,
  useKnowledgeMetadataFields,
  useKnowledgeSearch,
} from "@/core/knowledge/hooks";
import { formatKnowledgeSourcePosition } from "@/core/knowledge/source-position";
import type {
  KnowledgeBaseItem,
  KnowledgeMetadataFieldItem,
  KnowledgeMetadataFilterInput,
  KnowledgeMetadataFilterOperator,
  KnowledgeSearchInput,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";

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
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const search = useKnowledgeSearch(scope);
  const metadataFields = useKnowledgeMetadataFields(scope, base.id);
  const [query, setQuery] = useState("");
  // Empty inputs defer to the base defaults resolved server-side.
  const [topK, setTopK] = useState("");
  const [threshold, setThreshold] = useState("");
  const [filters, setFilters] = useState<FilterDraft[]>([]);
  const [nextFilterKey, setNextFilterKey] = useState(1);

  // Rebinding or clearing the reranker changes what the scores mean; stale
  // results must not sit next to the new setting. Search again to compare.
  const rerankerBinding = base.reranker_model_id ?? null;
  const resetSearch = search.reset;
  useEffect(() => {
    resetSearch();
  }, [rerankerBinding, resetSearch]);

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
      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!query.trim() || !topKValid || !thresholdValid || !filtersValid)
            return;
          const input: KnowledgeSearchInput = {
            query: query.trim(),
            knowledge_base_ids: [base.id],
          };
          if (parsedTopK !== undefined) {
            input.top_k = parsedTopK;
          }
          if (parsedThreshold !== undefined) {
            input.score_threshold = parsedThreshold;
          }
          if (filterInputs.length > 0) {
            input.metadata_filters = filterInputs;
          }
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

      {search.error ? (
        <p role="alert" className="text-destructive text-sm">
          {knowledgeErrorMessage(search.error, labels.errors)}
        </p>
      ) : null}

      {search.data ? (
        search.data.citations.length === 0 ? (
          <p
            className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-sm"
            data-testid="knowledge-search-empty"
          >
            {labels.search.empty}
          </p>
        ) : (
          <section
            aria-label={labels.search.resultsTitle(
              search.data.citations.length,
            )}
            className="space-y-2"
          >
            <h3 className="text-sm font-semibold">
              {labels.search.resultsTitle(search.data.citations.length)}
            </h3>
            <ol className="grid gap-2" data-testid="knowledge-search-results">
              {search.data.citations.map((citation) => {
                const position = formatKnowledgeSourcePosition(
                  citation.source_position,
                  labels.sourcePosition,
                );
                return (
                  <li
                    key={citation.segment_id}
                    className="border-border rounded-xl border p-4"
                  >
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="text-foreground truncate text-sm font-medium">
                        {citation.document_name}
                      </span>
                      <span className="text-muted-foreground truncate text-xs">
                        {citation.knowledge_base_name}
                      </span>
                      <Badge variant="secondary" className="ml-auto shrink-0">
                        {labels.search.score(citation.score)}
                      </Badge>
                    </div>
                    <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-x-2 text-xs">
                      <span>
                        {labels.citations.segmentPosition(
                          citation.segment_position,
                        )}
                      </span>
                      {position ? <span>· {position}</span> : null}
                    </div>
                    <p className="text-muted-foreground mt-2 text-sm leading-6 whitespace-pre-wrap">
                      {citation.snippet}
                    </p>
                  </li>
                );
              })}
            </ol>
          </section>
        )
      ) : null}

      <RecentQueriesSection scope={scope} base={base} onPick={setQuery} />
    </section>
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
