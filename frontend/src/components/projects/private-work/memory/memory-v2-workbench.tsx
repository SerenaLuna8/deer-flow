"use client";

import {
  BrainCircuitIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  Clock3Icon,
  DatabaseIcon,
  DownloadIcon,
  HistoryIcon,
  InboxIcon,
  LoaderCircleIcon,
  PencilIcon,
  RotateCcwIcon,
  SearchIcon,
  Settings2Icon,
  ShieldAlertIcon,
  Trash2Icon,
} from "lucide-react";
import { useEffect, useId, useState } from "react";
import { toast } from "sonner";

import { ProjectPageHeader } from "@/components/projects/project-page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { GatewayApiError } from "@/core/api/errors";
import { useI18n } from "@/core/i18n/hooks";
import {
  MEMORY_V2_CATEGORY_MAX_LENGTH,
  MEMORY_V2_CONTENT_MAX_LENGTH,
  type MemoryV2Candidate,
  type MemoryV2Fact,
  type MemoryV2FactDetail,
  type MemoryV2FactListStatus,
  type MemoryV2Status,
} from "@/core/private-work/memory";
import { formatTimeAgo } from "@/core/utils/datetime";

type PageState<T> = {
  items: readonly T[];
  page: number;
  hasNext: boolean;
  isLoading: boolean;
  isFetching: boolean;
  error: Error | null;
  retry: () => void;
  previous: () => void;
  next: () => void;
};

type FactPageState = PageState<MemoryV2Fact> & {
  query: string;
  category: string;
  status: MemoryV2FactListStatus;
  setQuery: (value: string) => void;
  setCategory: (value: string) => void;
  setStatus: (value: MemoryV2FactListStatus) => void;
};

type DetailState = {
  selectedFactId: string | null;
  data: MemoryV2FactDetail | null;
  isLoading: boolean;
  error: Error | null;
  retry: () => void;
  select: (factId: string) => void;
};

type StatusState = {
  data: MemoryV2Status | null;
  isLoading: boolean;
  error: Error | null;
  retry: () => void;
};

type WorkbenchActions = {
  canManage: boolean;
  canHardForget: boolean;
  canExport: boolean;
  isExporting: boolean;
  busyCandidateIds: readonly string[];
  busyFactIds: readonly string[];
  exportMemory: () => Promise<void>;
  acceptCandidate: (candidate: MemoryV2Candidate) => Promise<void>;
  rejectCandidate: (candidate: MemoryV2Candidate) => Promise<void>;
  reviseFact: (
    fact: MemoryV2Fact,
    input: {
      content?: string;
      category?: string;
      confidence?: number;
      reason?: string;
    },
  ) => Promise<void>;
  disableFact: (fact: MemoryV2Fact) => Promise<void>;
  restoreFact: (fact: MemoryV2Fact) => Promise<void>;
  hardForgetFact: (fact: MemoryV2Fact) => Promise<void>;
};

export type MemoryV2WorkbenchProps = {
  projectName: string;
  projectSlug: string;
  initialTab?: MemoryTab;
  facts: FactPageState;
  candidates: PageState<MemoryV2Candidate>;
  detail: DetailState;
  status: StatusState;
  actions: WorkbenchActions;
};

export type MemoryTab = "facts" | "candidates" | "history" | "settings";

function mutationMessage(error: unknown, conflict: string) {
  if (error instanceof GatewayApiError && error.status === 409) return conflict;
  return error instanceof Error ? error.message : String(error);
}

function isMemoryConflict(error: unknown) {
  return error instanceof GatewayApiError && error.status === 409;
}

function LoadingRows() {
  return (
    <div aria-label="Loading" className="space-y-0">
      {[0, 1, 2].map((item) => (
        <div key={item} className="border-border border-b p-5 last:border-b-0">
          <div className="flex items-center gap-2">
            <Skeleton className="h-5 w-20 rounded-full" />
            <Skeleton className="h-5 w-16 rounded-full" />
          </div>
          <Skeleton className="mt-4 h-4 w-11/12" />
          <Skeleton className="mt-2 h-4 w-7/12" />
          <Skeleton className="mt-4 h-8 w-64" />
        </div>
      ))}
    </div>
  );
}

function ErrorState({
  title,
  retryLabel,
  retry,
}: {
  title: string;
  retryLabel: string;
  retry: () => void;
}) {
  return (
    <Alert variant="destructive" className="my-4">
      <ShieldAlertIcon aria-hidden />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription>
        <Button type="button" size="sm" variant="outline" onClick={retry}>
          {retryLabel}
        </Button>
      </AlertDescription>
    </Alert>
  );
}

function Pagination({
  page,
  hasNext,
  isFetching,
  error,
  previous,
  next,
  retry,
}: Pick<
  PageState<unknown>,
  "page" | "hasNext" | "isFetching" | "error" | "previous" | "next" | "retry"
>) {
  const { t } = useI18n();
  const copy = t.settings.memory.v2;
  return (
    <div className="border-border flex flex-col gap-3 border-t px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      {error ? (
        <Button type="button" size="sm" variant="outline" onClick={retry}>
          {copy.retry}
        </Button>
      ) : (
        <span className="text-muted-foreground inline-flex items-center gap-2 text-xs">
          {isFetching ? (
            <LoaderCircleIcon aria-hidden className="size-3.5 animate-spin" />
          ) : null}
          {copy.pagination.page(page + 1)}
        </span>
      )}
      <div className="flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={page === 0 || isFetching}
          onClick={previous}
        >
          <ChevronLeftIcon aria-hidden />
          {copy.pagination.previous}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!hasNext || isFetching}
          onClick={next}
        >
          {copy.pagination.next}
          <ChevronRightIcon aria-hidden />
        </Button>
      </div>
    </div>
  );
}

function FactsPanel({
  facts,
  actions,
  openHistory,
  openEdit,
  openForget,
}: {
  facts: FactPageState;
  actions: WorkbenchActions;
  openHistory: (fact: MemoryV2Fact) => void;
  openEdit: (fact: MemoryV2Fact) => void;
  openForget: (fact: MemoryV2Fact) => void;
}) {
  const { locale, t } = useI18n();
  const copy = t.settings.memory.v2;
  const hasFilters =
    facts.query.trim().length > 0 ||
    facts.category.trim().length > 0 ||
    facts.status !== "active";

  async function changeState(fact: MemoryV2Fact) {
    try {
      if (fact.status === "active") {
        await actions.disableFact(fact);
        toast.success(copy.disableSuccess);
      } else {
        await actions.restoreFact(fact);
        toast.success(copy.restoreSuccess);
      }
    } catch (error) {
      toast.error(mutationMessage(error, copy.conflict));
    }
  }

  return (
    <section aria-labelledby="memory-v2-facts-title" className="pt-3">
      <div>
        <h2 id="memory-v2-facts-title" className="text-lg font-semibold">
          {copy.facts.title}
        </h2>
        <p className="text-muted-foreground mt-1 max-w-3xl text-sm leading-6">
          {copy.facts.description}
        </p>
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-[minmax(0,1fr)_220px_180px]">
        <label className="relative block">
          <span className="sr-only">{copy.facts.searchPlaceholder}</span>
          <SearchIcon
            aria-hidden
            className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
          />
          <Input
            value={facts.query}
            className="pl-9"
            placeholder={copy.facts.searchPlaceholder}
            onChange={(event) => facts.setQuery(event.target.value)}
          />
        </label>
        <label>
          <span className="sr-only">{copy.facts.categoryPlaceholder}</span>
          <Input
            value={facts.category}
            placeholder={copy.facts.categoryPlaceholder}
            onChange={(event) => facts.setCategory(event.target.value)}
          />
        </label>
        <Select
          value={facts.status}
          onValueChange={(value) =>
            facts.setStatus(value as MemoryV2FactListStatus)
          }
        >
          <SelectTrigger className="w-full" aria-label={copy.facts.statusAll}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="active">{copy.facts.statusActive}</SelectItem>
            <SelectItem value="disabled">
              {copy.facts.statusDisabled}
            </SelectItem>
            <SelectItem value="all">{copy.facts.statusAll}</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div
        aria-busy={facts.isFetching}
        className="border-border mt-4 overflow-hidden rounded-xl border"
      >
        {facts.isLoading ? (
          <LoadingRows />
        ) : facts.error && facts.items.length === 0 ? (
          <div className="px-5">
            <ErrorState
              title={copy.facts.loadError}
              retryLabel={copy.retry}
              retry={facts.retry}
            />
          </div>
        ) : facts.items.length === 0 ? (
          <>
            <Empty className="min-h-72 border-0">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <DatabaseIcon aria-hidden />
                </EmptyMedia>
                <EmptyTitle>
                  {hasFilters
                    ? copy.facts.noMatchesTitle
                    : copy.facts.emptyTitle}
                </EmptyTitle>
                <EmptyDescription>
                  {hasFilters
                    ? copy.facts.noMatchesDescription
                    : copy.facts.emptyDescription}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
            {facts.page > 0 ? <Pagination {...facts} /> : null}
          </>
        ) : (
          <div>
            <div className="bg-muted/35 border-border border-b px-5 py-2.5 text-xs font-medium">
              {copy.facts.count(facts.items.length)}
            </div>
            {facts.items.map((fact) => {
              const busy = actions.busyFactIds.includes(fact.id);
              return (
                <article
                  key={fact.id}
                  className="border-border border-b p-5 last:border-b-0"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">
                      {fact.currentRevision.category}
                    </Badge>
                    <Badge
                      variant={
                        fact.status === "active" ? "secondary" : "outline"
                      }
                    >
                      {fact.status === "active"
                        ? copy.facts.statusActive
                        : copy.facts.statusDisabled}
                    </Badge>
                    <span className="text-muted-foreground text-xs">
                      {copy.facts.confidence} {fact.currentRevision.confidence}
                    </span>
                  </div>
                  <p className="mt-3 text-sm leading-6 whitespace-pre-wrap">
                    {fact.currentRevision.content ?? copy.emptyContent}
                  </p>
                  <div className="text-muted-foreground mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs">
                    <span>{copy.facts.version(fact.version)}</span>
                    <span>
                      {copy.facts.updated(
                        formatTimeAgo(fact.updatedAt, locale),
                      )}
                    </span>
                  </div>
                  <div className="mt-4 flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => openHistory(fact)}
                    >
                      <HistoryIcon aria-hidden />
                      {copy.facts.viewHistory}
                    </Button>
                    {actions.canManage ? (
                      <>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => openEdit(fact)}
                        >
                          <PencilIcon aria-hidden />
                          {copy.facts.edit}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => void changeState(fact)}
                        >
                          {fact.status === "active" ? (
                            <Clock3Icon aria-hidden />
                          ) : (
                            <RotateCcwIcon aria-hidden />
                          )}
                          {fact.status === "active"
                            ? copy.facts.disable
                            : copy.facts.restore}
                        </Button>
                      </>
                    ) : null}
                    {actions.canHardForget ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="text-destructive hover:text-destructive"
                        disabled={busy}
                        onClick={() => openForget(fact)}
                      >
                        <Trash2Icon aria-hidden />
                        {copy.facts.hardForget}
                      </Button>
                    ) : null}
                  </div>
                </article>
              );
            })}
            <Pagination {...facts} />
          </div>
        )}
      </div>
    </section>
  );
}

function CandidatesPanel({
  candidates,
  actions,
}: {
  candidates: PageState<MemoryV2Candidate>;
  actions: WorkbenchActions;
}) {
  const { locale, t } = useI18n();
  const copy = t.settings.memory.v2;

  async function decide(
    candidate: MemoryV2Candidate,
    action: "accept" | "reject",
  ) {
    try {
      if (action === "accept") {
        await actions.acceptCandidate(candidate);
        toast.success(copy.acceptSuccess);
      } else {
        await actions.rejectCandidate(candidate);
        toast.success(copy.rejectSuccess);
      }
    } catch (error) {
      toast.error(mutationMessage(error, copy.conflict));
    }
  }

  return (
    <section aria-labelledby="memory-v2-candidates-title" className="pt-3">
      <h2 id="memory-v2-candidates-title" className="text-lg font-semibold">
        {copy.candidates.title}
      </h2>
      <p className="text-muted-foreground mt-1 max-w-3xl text-sm leading-6">
        {copy.candidates.description}
      </p>
      <div
        aria-busy={candidates.isFetching}
        className="border-border mt-5 overflow-hidden rounded-xl border"
      >
        {candidates.isLoading ? (
          <LoadingRows />
        ) : candidates.error && candidates.items.length === 0 ? (
          <div className="px-5">
            <ErrorState
              title={copy.candidates.loadError}
              retryLabel={copy.retry}
              retry={candidates.retry}
            />
          </div>
        ) : candidates.items.length === 0 ? (
          <>
            <Empty className="min-h-72 border-0">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <InboxIcon aria-hidden />
                </EmptyMedia>
                <EmptyTitle>{copy.candidates.emptyTitle}</EmptyTitle>
                <EmptyDescription>
                  {copy.candidates.emptyDescription}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
            {candidates.page > 0 ? <Pagination {...candidates} /> : null}
          </>
        ) : (
          <div>
            <div className="bg-muted/35 border-border border-b px-5 py-2.5 text-xs font-medium">
              {copy.candidates.count(candidates.items.length)}
            </div>
            {candidates.items.map((candidate) => {
              const busy = actions.busyCandidateIds.includes(candidate.id);
              const canAccept =
                actions.canManage &&
                candidate.sensitivity === "normal" &&
                candidate.content !== null &&
                candidate.contentErasedAt === null;
              return (
                <article
                  key={candidate.id}
                  className="border-border border-b p-5 last:border-b-0"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">{candidate.candidateType}</Badge>
                    <Badge variant="secondary">
                      {
                        copy.candidates.retentionLabels[
                          candidate.retentionClass
                        ]
                      }
                    </Badge>
                    <Badge
                      variant={
                        candidate.sensitivity === "normal"
                          ? "outline"
                          : "destructive"
                      }
                    >
                      {copy.candidates.sensitivityLabels[candidate.sensitivity]}
                    </Badge>
                  </div>
                  <p className="mt-3 text-sm leading-6 whitespace-pre-wrap">
                    {candidate.content ?? copy.emptyContent}
                  </p>
                  <div className="text-muted-foreground mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs">
                    <span>
                      {copy.facts.confidence} {candidate.confidence}
                    </span>
                    <span>
                      {copy.candidates.created(
                        formatTimeAgo(candidate.createdAt, locale),
                      )}
                    </span>
                  </div>
                  {!canAccept && actions.canManage ? (
                    <p className="text-muted-foreground mt-3 text-xs">
                      {copy.candidates.cannotAccept}
                    </p>
                  ) : null}
                  {actions.canManage ? (
                    <div className="mt-4 flex flex-wrap gap-2">
                      <Button
                        type="button"
                        size="sm"
                        disabled={!canAccept || busy}
                        onClick={() => void decide(candidate, "accept")}
                      >
                        {copy.candidates.accept}
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() => void decide(candidate, "reject")}
                      >
                        {copy.candidates.reject}
                      </Button>
                    </div>
                  ) : null}
                </article>
              );
            })}
            <Pagination {...candidates} />
          </div>
        )}
      </div>
    </section>
  );
}

function HistoryPanel({
  projectSlug,
  detail,
}: {
  projectSlug: string;
  detail: DetailState;
}) {
  const { locale, t } = useI18n();
  const copy = t.settings.memory.v2;
  const actorLabels = {
    user: locale === "zh-CN" ? "用户" : "user",
    system: locale === "zh-CN" ? "系统" : "system",
    consolidator: locale === "zh-CN" ? "整理器" : "consolidator",
  } as const;

  return (
    <section aria-labelledby="memory-v2-history-title" className="pt-3">
      <h2 id="memory-v2-history-title" className="text-lg font-semibold">
        {copy.history.title}
      </h2>
      <p className="text-muted-foreground mt-1 max-w-3xl text-sm leading-6">
        {copy.history.description}
      </p>
      <div className="border-border mt-5 rounded-xl border">
        {!detail.selectedFactId ? (
          <Empty className="min-h-72 border-0">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <HistoryIcon aria-hidden />
              </EmptyMedia>
              <EmptyTitle>{copy.history.selectTitle}</EmptyTitle>
              <EmptyDescription>
                {copy.history.selectDescription}
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : detail.isLoading ? (
          <LoadingRows />
        ) : detail.error || !detail.data ? (
          <div className="px-5">
            <ErrorState
              title={copy.history.loadError}
              retryLabel={copy.retry}
              retry={detail.retry}
            />
          </div>
        ) : (
          <div className="p-5 sm:p-6">
            <div className="bg-muted/40 rounded-xl p-4">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline">
                  {detail.data.fact.currentRevision.category}
                </Badge>
                <Badge variant="secondary">
                  {detail.data.fact.status === "active"
                    ? copy.facts.statusActive
                    : copy.facts.statusDisabled}
                </Badge>
                <span className="text-muted-foreground text-xs">
                  {copy.history.current}
                </span>
              </div>
              <p className="mt-3 text-sm leading-6 whitespace-pre-wrap">
                {detail.data.fact.currentRevision.content ?? copy.emptyContent}
              </p>
            </div>

            <h3 className="mt-7 text-sm font-semibold">
              {copy.history.revisions}
            </h3>
            <div className="mt-3 space-y-4">
              {detail.data.revisions.map((revision) => {
                const evidence = detail.data?.evidence.filter(
                  (item) => item.revisionId === revision.id,
                );
                return (
                  <article
                    key={revision.id}
                    className="border-border rounded-xl border p-4"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium">
                          {copy.history.revision(revision.revisionNumber)}
                        </span>
                        <Badge variant="outline">{revision.category}</Badge>
                      </div>
                      <span className="text-muted-foreground text-xs">
                        {formatTimeAgo(revision.createdAt, locale)}
                      </span>
                    </div>
                    <p className="mt-3 text-sm leading-6 whitespace-pre-wrap">
                      {revision.content ?? copy.emptyContent}
                    </p>
                    <div className="text-muted-foreground mt-3 space-y-1 text-xs">
                      <p>
                        {copy.history.changedBy(
                          actorLabels[revision.changedBy],
                        )}
                      </p>
                      {revision.changeReason ? (
                        <p>{copy.history.reason(revision.changeReason)}</p>
                      ) : null}
                    </div>
                    <div className="border-border mt-4 border-t pt-4">
                      <p className="text-xs font-medium">
                        {copy.history.evidence}
                      </p>
                      {evidence && evidence.length > 0 ? (
                        <div className="mt-2 space-y-2">
                          {evidence.map((item) => {
                            const sourceDeleted = item.sourceErasedAt !== null;
                            return (
                              <div
                                key={item.id}
                                className="bg-muted/35 rounded-lg p-3 text-xs"
                              >
                                <div className="flex flex-wrap items-center gap-2">
                                  <Badge
                                    variant={
                                      sourceDeleted ? "destructive" : "outline"
                                    }
                                  >
                                    {sourceDeleted
                                      ? copy.history.sourceDeleted
                                      : item.threadId
                                        ? copy.history.sourceAvailable
                                        : copy.history.sourceUnknown}
                                  </Badge>
                                  <span className="text-muted-foreground">
                                    {item.trustClass}
                                  </span>
                                </div>
                                {item.evidenceExcerpt ? (
                                  <p className="mt-2 leading-5 whitespace-pre-wrap">
                                    {item.evidenceExcerpt}
                                  </p>
                                ) : null}
                                {!sourceDeleted && item.threadId ? (
                                  <a
                                    className="text-primary mt-2 inline-flex underline underline-offset-4"
                                    href={`/projects/${encodeURIComponent(projectSlug)}/chats/${encodeURIComponent(item.threadId)}`}
                                  >
                                    {copy.history.openThread}
                                  </a>
                                ) : null}
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <p className="text-muted-foreground mt-2 text-xs">
                          {copy.history.noEvidence}
                        </p>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function SettingsPanel({ status }: { status: StatusState }) {
  const { t } = useI18n();
  const copy = t.settings.memory.v2;
  const valueClass = "mt-1 text-base font-semibold";
  return (
    <section aria-labelledby="memory-v2-settings-title" className="pt-3">
      <h2 id="memory-v2-settings-title" className="text-lg font-semibold">
        {copy.settings.title}
      </h2>
      <p className="text-muted-foreground mt-1 max-w-3xl text-sm leading-6">
        {copy.settings.description}
      </p>
      {status.isLoading ? (
        <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2, 3, 4, 5].map((item) => (
            <Skeleton key={item} className="h-24 rounded-xl" />
          ))}
        </div>
      ) : status.error || !status.data ? (
        <ErrorState
          title={copy.settings.loadError}
          retryLabel={copy.retry}
          retry={status.retry}
        />
      ) : (
        <>
          <dl className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <div className="border-border rounded-xl border p-4">
              <dt className="text-muted-foreground text-xs">
                {copy.settings.pipeline}
              </dt>
              <dd className={valueClass}>
                {copy.settings.modes[status.data.pipelineMode]}
              </dd>
            </div>
            <div className="border-border rounded-xl border p-4">
              <dt className="text-muted-foreground text-xs">
                {t.settings.memory.title}
              </dt>
              <dd className={valueClass}>
                {status.data.enabled
                  ? copy.settings.enabled
                  : copy.settings.disabled}
              </dd>
            </div>
            <div className="border-border rounded-xl border p-4">
              <dt className="text-muted-foreground text-xs">
                {copy.settings.search}
              </dt>
              <dd className={valueClass}>
                {status.data.searchEnabled
                  ? copy.settings.enabled
                  : copy.settings.disabled}
              </dd>
            </div>
            <div className="border-border rounded-xl border p-4">
              <dt className="text-muted-foreground text-xs">
                {copy.settings.injection}
              </dt>
              <dd className={valueClass}>
                {status.data.injectionEnabled
                  ? copy.settings.enabled
                  : copy.settings.disabled}
              </dd>
            </div>
            <div className="border-border rounded-xl border p-4">
              <dt className="text-muted-foreground text-xs">
                {copy.settings.consolidationInterval}
              </dt>
              <dd className={valueClass}>
                {copy.settings.minutes(
                  status.data.consolidationIntervalMinutes,
                )}
              </dd>
            </div>
            <div className="border-border rounded-xl border p-4">
              <dt className="text-muted-foreground text-xs">
                {copy.settings.retention}
              </dt>
              <dd className={valueClass}>
                {copy.settings.days(status.data.candidateRetentionDays)}
              </dd>
            </div>
          </dl>
          <p className="text-muted-foreground mt-4 text-xs">
            {copy.settings.readOnly}
          </p>
        </>
      )}
    </section>
  );
}

function EditFactDialog({
  fact,
  pending,
  onClose,
  onSave,
}: {
  fact: MemoryV2Fact | null;
  pending: boolean;
  onClose: () => void;
  onSave: WorkbenchActions["reviseFact"];
}) {
  const { t } = useI18n();
  const copy = t.settings.memory.v2;
  const contentId = useId();
  const categoryId = useId();
  const confidenceId = useId();
  const reasonId = useId();
  const [content, setContent] = useState("");
  const [category, setCategory] = useState("");
  const [confidence, setConfidence] = useState("");
  const [reason, setReason] = useState("");

  useEffect(() => {
    setContent(fact?.currentRevision.content ?? "");
    setCategory(fact?.currentRevision.category ?? "");
    setConfidence(fact ? String(fact.currentRevision.confidence) : "");
    setReason("");
  }, [fact]);

  async function save() {
    if (!fact) return;
    const normalizedContent = content.trim();
    const normalizedCategory = category.trim();
    const parsedConfidence = Number(confidence);
    if (!normalizedContent) {
      toast.error(copy.edit.invalidContent);
      return;
    }
    if (!normalizedCategory) {
      toast.error(copy.edit.invalidCategory);
      return;
    }
    if (
      !Number.isFinite(parsedConfidence) ||
      parsedConfidence < 0 ||
      parsedConfidence > 1
    ) {
      toast.error(copy.edit.invalidConfidence);
      return;
    }
    const input: Parameters<WorkbenchActions["reviseFact"]>[1] = {};
    if (normalizedContent !== fact.currentRevision.content) {
      input.content = normalizedContent;
    }
    if (normalizedCategory !== fact.currentRevision.category) {
      input.category = normalizedCategory;
    }
    if (parsedConfidence !== fact.currentRevision.confidence) {
      input.confidence = parsedConfidence;
    }
    if (
      input.content === undefined &&
      input.category === undefined &&
      input.confidence === undefined
    ) {
      onClose();
      return;
    }
    const normalizedReason = reason.trim();
    if (normalizedReason) input.reason = normalizedReason;
    try {
      await onSave(fact, input);
      toast.success(copy.edit.success);
      onClose();
    } catch (error) {
      if (isMemoryConflict(error)) onClose();
      toast.error(mutationMessage(error, copy.conflict));
    }
  }

  return (
    <Dialog open={fact !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{copy.edit.title}</DialogTitle>
          <DialogDescription>{copy.edit.description}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 py-2">
          <div className="grid gap-2">
            <label htmlFor={contentId} className="text-sm font-medium">
              {copy.edit.content}
            </label>
            <Textarea
              id={contentId}
              rows={6}
              value={content}
              maxLength={MEMORY_V2_CONTENT_MAX_LENGTH}
              disabled={pending}
              onChange={(event) => setContent(event.target.value)}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-2">
              <label htmlFor={categoryId} className="text-sm font-medium">
                {copy.edit.category}
              </label>
              <Input
                id={categoryId}
                value={category}
                maxLength={MEMORY_V2_CATEGORY_MAX_LENGTH}
                disabled={pending}
                onChange={(event) => setCategory(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <label htmlFor={confidenceId} className="text-sm font-medium">
                {copy.edit.confidence}
              </label>
              <Input
                id={confidenceId}
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={confidence}
                disabled={pending}
                onChange={(event) => setConfidence(event.target.value)}
              />
            </div>
          </div>
          <div className="grid gap-2">
            <label htmlFor={reasonId} className="text-sm font-medium">
              {copy.edit.reason}
            </label>
            <Input
              id={reasonId}
              value={reason}
              maxLength={64}
              disabled={pending}
              placeholder={copy.edit.reasonPlaceholder}
              onChange={(event) => setReason(event.target.value)}
            />
          </div>
        </div>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={onClose}
          >
            {copy.edit.cancel}
          </Button>
          <Button type="button" disabled={pending} onClick={() => void save()}>
            {pending ? (
              <LoaderCircleIcon aria-hidden className="animate-spin" />
            ) : null}
            {copy.edit.save}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ForgetFactDialog({
  fact,
  pending,
  onClose,
  onForget,
}: {
  fact: MemoryV2Fact | null;
  pending: boolean;
  onClose: () => void;
  onForget: WorkbenchActions["hardForgetFact"];
}) {
  const { t } = useI18n();
  const copy = t.settings.memory.v2;

  async function forget() {
    if (!fact) return;
    try {
      await onForget(fact);
      toast.success(copy.forget.success);
      onClose();
    } catch (error) {
      if (isMemoryConflict(error)) onClose();
      toast.error(mutationMessage(error, copy.conflict));
    }
  }

  return (
    <Dialog open={fact !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{copy.forget.title}</DialogTitle>
          <DialogDescription>{copy.forget.description}</DialogDescription>
        </DialogHeader>
        <div className="bg-muted/45 rounded-xl p-4">
          <p className="text-muted-foreground text-xs font-medium">
            {copy.forget.preview}
          </p>
          <p className="mt-2 line-clamp-4 text-sm leading-6 whitespace-pre-wrap">
            {fact?.currentRevision.content ?? copy.emptyContent}
          </p>
        </div>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={onClose}
          >
            {copy.forget.cancel}
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={pending}
            onClick={() => void forget()}
          >
            {pending ? (
              <LoaderCircleIcon aria-hidden className="animate-spin" />
            ) : (
              <Trash2Icon aria-hidden />
            )}
            {copy.forget.confirm}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function MemoryV2Workbench({
  projectName,
  projectSlug,
  initialTab = "facts",
  facts,
  candidates,
  detail,
  status,
  actions,
}: MemoryV2WorkbenchProps) {
  const { t } = useI18n();
  const copy = t.settings.memory.v2;
  const [tab, setTab] = useState<MemoryTab>(initialTab);
  const [editingFact, setEditingFact] = useState<MemoryV2Fact | null>(null);
  const [forgettingFact, setForgettingFact] = useState<MemoryV2Fact | null>(
    null,
  );

  function openHistory(fact: MemoryV2Fact) {
    detail.select(fact.id);
    setTab("history");
  }

  async function exportMemory() {
    try {
      await actions.exportMemory();
      toast.success(copy.exportSuccess);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <ProjectPageHeader
        eyebrow={projectName}
        title={t.settings.memory.title}
        description={copy.description}
        icon={<BrainCircuitIcon aria-hidden className="size-4" />}
        actions={
          actions.canExport ? (
            <Button
              type="button"
              variant="outline"
              disabled={actions.isExporting}
              onClick={() => void exportMemory()}
            >
              {actions.isExporting ? (
                <LoaderCircleIcon aria-hidden className="animate-spin" />
              ) : (
                <DownloadIcon aria-hidden />
              )}
              {copy.export}
            </Button>
          ) : null
        }
      />

      <Tabs
        value={tab}
        className="mt-7"
        onValueChange={(value) => setTab(value as MemoryTab)}
      >
        <div className="overflow-x-auto pb-1">
          <TabsList variant="line" className="min-w-max">
            <TabsTrigger value="facts" className="px-3">
              <DatabaseIcon aria-hidden />
              {copy.tabs.facts}
            </TabsTrigger>
            <TabsTrigger value="candidates" className="px-3">
              <InboxIcon aria-hidden />
              {copy.tabs.candidates}
            </TabsTrigger>
            <TabsTrigger value="history" className="px-3">
              <HistoryIcon aria-hidden />
              {copy.tabs.history}
            </TabsTrigger>
            <TabsTrigger value="settings" className="px-3">
              <Settings2Icon aria-hidden />
              {copy.tabs.settings}
            </TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="facts">
          <FactsPanel
            facts={facts}
            actions={actions}
            openHistory={openHistory}
            openEdit={setEditingFact}
            openForget={setForgettingFact}
          />
        </TabsContent>
        <TabsContent value="candidates">
          <CandidatesPanel candidates={candidates} actions={actions} />
        </TabsContent>
        <TabsContent value="history">
          <HistoryPanel projectSlug={projectSlug} detail={detail} />
        </TabsContent>
        <TabsContent value="settings">
          <SettingsPanel status={status} />
        </TabsContent>
      </Tabs>

      <EditFactDialog
        fact={editingFact}
        pending={Boolean(
          editingFact && actions.busyFactIds.includes(editingFact.id),
        )}
        onClose={() => setEditingFact(null)}
        onSave={actions.reviseFact}
      />
      <ForgetFactDialog
        fact={forgettingFact}
        pending={Boolean(
          forgettingFact && actions.busyFactIds.includes(forgettingFact.id),
        )}
        onClose={() => setForgettingFact(null)}
        onForget={actions.hardForgetFact}
      />
    </main>
  );
}
