"use client";

import {
  ArrowLeftIcon,
  ChevronRightIcon,
  GripIcon,
  LocateIcon,
  PencilIcon,
  PlusIcon,
  Trash2Icon,
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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import {
  useCreateKnowledgeSegment,
  useDeleteKnowledgeSegment,
  useKnowledgeDocumentSegments,
  useKnowledgeSegmentLocate,
  useUpdateKnowledgeSegment,
} from "@/core/knowledge/hooks";
import { formatKnowledgeSourcePosition } from "@/core/knowledge/source-position";
import type {
  KnowledgeBaseItem,
  KnowledgeDocumentItem,
  KnowledgeSegmentItem,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";
import { cn } from "@/lib/utils";

import { knowledgeErrorMessage } from "./knowledge-error";
import { KnowledgeFileTypeIcon } from "./knowledge-file-type-icon";
import { KnowledgeSummaryBlock } from "./knowledge-summary-block";

/** Mirrors the backend's segment content ceiling (splitter's largest chunk). */
const MAX_SEGMENT_CONTENT_CHARS = 4000;

/**
 * Full segment browsing page for one document: list, enable toggles, edit,
 * manual add, and delete. Replaces the former read-only preview modal.
 *
 * `locateSegmentId` (the URL's `segment=`) resolves through the segment
 * detail endpoint — never by walking the base's pages — and renders a
 * pinned card. A deleted segment or a cross-base id combination shows an
 * explicit failure instead of resurrecting a cached object.
 */
export function KnowledgeSegmentsBrowser({
  scope,
  base,
  document,
  canEdit,
  onBack,
  locateSegmentId = null,
  onDismissLocate,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem;
  canEdit: boolean;
  onBack: () => void;
  locateSegmentId?: string | null;
  onDismissLocate?: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const [page, setPage] = useState(1);
  const segments = useKnowledgeDocumentSegments(scope, document.id, page);
  // Toggles surface errors at the list level; the edit dialog owns its own
  // mutation instance so its inline error does not leak onto the toggles.
  const toggleSegment = useUpdateKnowledgeSegment(scope);
  const [adding, setAdding] = useState(false);
  const [viewing, setViewing] = useState<KnowledgeSegmentItem | null>(null);
  const [editing, setEditing] = useState<KnowledgeSegmentItem | null>(null);
  const [deleting, setDeleting] = useState<KnowledgeSegmentItem | null>(null);

  const total = segments.data?.total ?? 0;
  const pageSize = segments.data?.page_size ?? 1;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  // Deleting the last segment of the last page must not strand the browser
  // on an empty page.
  useEffect(() => {
    if (segments.data && page > pageCount) setPage(pageCount);
  }, [segments.data, page, pageCount]);

  const mutationScope = { documentId: document.id, baseId: base.id };

  return (
    <section
      aria-label={labels.segments.title(document.name)}
      data-testid="knowledge-segment-browser"
      className="space-y-4 text-[13px]"
    >
      <div className="border-border/60 flex flex-col gap-3 border-b pb-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground hover:text-foreground shrink-0 rounded-md"
            aria-label={labels.detail.documents}
            title={labels.detail.documents}
            onClick={onBack}
          >
            <ArrowLeftIcon aria-hidden className="size-3.5" />
          </Button>
          <KnowledgeFileTypeIcon fileName={document.original_name} />
          <div className="min-w-0">
            <h2
              className="truncate text-[13px] font-semibold"
              title={document.name}
            >
              {document.name}
            </h2>
            <p className="text-muted-foreground mt-0.5 text-xs">
              {labels.segments.stats(
                document.segment_count,
                document.word_count,
              )}
            </p>
          </div>
        </div>
        {canEdit && document.status === "ready" ? (
          <Button
            type="button"
            className="h-8 rounded-md bg-blue-600 text-[13px] text-white hover:bg-blue-700 focus-visible:ring-blue-300"
            onClick={() => setAdding(true)}
          >
            <PlusIcon aria-hidden className="size-4" />
            {labels.segments.add}
          </Button>
        ) : null}
      </div>

      {toggleSegment.error ? (
        <p role="alert" className="text-destructive text-[13px]">
          {knowledgeErrorMessage(toggleSegment.error, labels.errors)}
        </p>
      ) : null}

      {locateSegmentId !== null ? (
        <SegmentLocateCard
          scope={scope}
          base={base}
          document={document}
          segmentId={locateSegmentId}
          onDismiss={onDismissLocate}
        />
      ) : null}

      {segments.isLoading ? (
        <Skeleton className="h-40 rounded-lg" />
      ) : segments.error ? (
        <p role="alert" className="text-destructive text-[13px]">
          {knowledgeErrorMessage(segments.error, labels.errors)}
        </p>
      ) : (segments.data?.items.length ?? 0) === 0 ? (
        <p className="text-muted-foreground border-border/70 bg-muted/10 rounded-lg border border-dashed px-4 py-10 text-center text-[13px]">
          {labels.segments.empty}
        </p>
      ) : (
        <ol
          className="divide-border/50 divide-y"
          data-testid="knowledge-segment-list"
        >
          {segments.data?.items.map((segment) => {
            const position = formatKnowledgeSourcePosition(
              segment.source_position,
              labels.sourcePosition,
            );
            const manual = segment.source_position.manual === true;
            return (
              <li
                key={segment.id}
                className={cn(
                  "hover:bg-muted/40 group rounded-md px-3 py-3 transition-colors",
                  (editing?.id === segment.id || viewing?.id === segment.id) &&
                    "bg-muted/50",
                )}
              >
                <div className="text-muted-foreground flex min-h-7 flex-wrap items-center gap-1.5 text-xs">
                  <span
                    className={cn(
                      "inline-flex items-center gap-1 font-medium",
                      (editing?.id === segment.id ||
                        viewing?.id === segment.id) &&
                        "text-blue-600 dark:text-blue-400",
                    )}
                  >
                    <GripIcon aria-hidden className="size-3" />
                    {labels.segments.position(segment.position)}
                  </span>
                  {position ? <span>· {position}</span> : null}
                  <span>· {labels.segments.wordCount(segment.word_count)}</span>
                  {manual ? (
                    <Badge
                      variant="outline"
                      className="bg-muted text-muted-foreground rounded-sm border-transparent px-1.5 py-0 text-[11px] font-normal"
                    >
                      {labels.segments.manualBadge}
                    </Badge>
                  ) : null}
                  {!segment.enabled ? (
                    <Badge
                      variant="secondary"
                      className="rounded-sm px-1.5 py-0 text-[11px] font-normal"
                    >
                      {labels.status.disabled}
                    </Badge>
                  ) : null}
                  <div className="ml-auto flex items-center gap-1">
                    {canEdit ? (
                      <>
                        <Switch
                          className="mr-1 data-[state=checked]:bg-blue-600"
                          checked={segment.enabled}
                          disabled={toggleSegment.isPending}
                          aria-label={
                            segment.enabled
                              ? labels.segments.disableAria(segment.position)
                              : labels.segments.enableAria(segment.position)
                          }
                          onCheckedChange={(checked) =>
                            toggleSegment.mutate({
                              segmentId: segment.id,
                              ...mutationScope,
                              input: { enabled: checked },
                            })
                          }
                        />
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="text-muted-foreground h-7 rounded-md px-2 text-xs hover:text-blue-600"
                          onClick={() => setEditing(segment)}
                        >
                          <PencilIcon aria-hidden className="size-3.5" />
                          {labels.segments.edit}
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="text-muted-foreground hover:bg-destructive/5 hover:text-destructive h-7 rounded-md px-2 text-xs"
                          onClick={() => setDeleting(segment)}
                        >
                          <Trash2Icon aria-hidden className="size-3.5" />
                          {labels.segments.delete}
                        </Button>
                      </>
                    ) : null}
                  </div>
                </div>
                <p
                  className={cn(
                    "mt-1 line-clamp-3 text-[13px] leading-6 [overflow-wrap:anywhere] whitespace-normal",
                    segment.enabled
                      ? "text-foreground/85"
                      : "text-muted-foreground",
                  )}
                >
                  {segment.content}
                </p>
                <button
                  type="button"
                  className="text-muted-foreground focus-visible:ring-ring mt-1.5 inline-flex cursor-pointer items-center gap-1 rounded-sm text-xs transition-colors hover:text-blue-600 focus-visible:ring-2 focus-visible:outline-none"
                  onClick={() => setViewing(segment)}
                >
                  <ChevronRightIcon aria-hidden className="size-3" />
                  {labels.segments.viewContent}
                </button>
              </li>
            );
          })}
        </ol>
      )}

      <div className="border-border/60 flex flex-wrap items-center justify-between gap-2 border-t pt-3">
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="bg-muted/60 h-8 rounded-md border-transparent text-xs shadow-none"
            disabled={page <= 1 || segments.isFetching}
            onClick={() => setPage((current) => Math.max(1, current - 1))}
          >
            {labels.segments.previousPage}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="bg-muted/60 h-8 rounded-md border-transparent text-xs shadow-none"
            disabled={page >= pageCount || segments.isFetching}
            onClick={() => setPage((current) => current + 1)}
          >
            {labels.segments.nextPage}
          </Button>
        </div>
        {segments.data ? (
          <p className="text-muted-foreground text-xs tabular-nums">
            {labels.segments.pageInfo(page, pageCount, total)}
          </p>
        ) : null}
      </div>

      {adding ? (
        <AddSegmentSheet
          scope={scope}
          base={base}
          document={document}
          onClose={() => setAdding(false)}
        />
      ) : null}

      {editing ? (
        <EditSegmentSheet
          key={editing.id}
          scope={scope}
          base={base}
          document={document}
          segment={editing}
          onClose={() => setEditing(null)}
        />
      ) : null}

      {viewing ? (
        <ViewSegmentSheet
          scope={scope}
          base={base}
          document={document}
          segment={viewing}
          onClose={() => setViewing(null)}
        />
      ) : null}

      {deleting ? (
        <DeleteSegmentDialog
          scope={scope}
          base={base}
          document={document}
          segment={deleting}
          onClose={() => setDeleting(null)}
        />
      ) : null}
    </section>
  );
}

/**
 * Pinned card for the URL-addressed segment. The detail read validates the
 * base/document/segment lineage server-side; any failure (deleted, foreign
 * combination, revoked access) renders an explicit notice — the card never
 * falls back to a previously cached object.
 */
function SegmentLocateCard({
  scope,
  base,
  document,
  segmentId,
  onDismiss,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem;
  segmentId: string;
  onDismiss?: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const locate = useKnowledgeSegmentLocate(scope, base.id, document, segmentId);

  return (
    <aside
      aria-label={labels.segments.locatedTitle(
        locate.data?.segment.position ?? 0,
      )}
      data-testid="knowledge-segment-locate"
      className="rounded-md border border-blue-200 bg-blue-50/50 p-3 dark:border-blue-900 dark:bg-blue-950/20"
    >
      {locate.isLoading ? (
        <Skeleton className="h-16 rounded-lg" />
      ) : locate.error !== null || locate.data === undefined ? (
        <div className="flex items-start gap-2">
          <p
            role="alert"
            className="text-destructive min-w-0 flex-1 text-[13px]"
          >
            {labels.segments.locateFailed}
          </p>
          {onDismiss ? (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-8 rounded-lg"
              aria-label={labels.segments.dismissLocate}
              onClick={onDismiss}
            >
              <XIcon aria-hidden className="size-4" />
            </Button>
          ) : null}
        </div>
      ) : (
        <div className="space-y-1.5">
          <div className="text-muted-foreground flex items-center gap-2 text-xs">
            <LocateIcon
              aria-hidden
              className="size-3.5 text-blue-600 dark:text-blue-400"
            />
            <span className="text-foreground font-medium">
              {labels.segments.locatedTitle(locate.data.segment.position)}
            </span>
            <span>
              · {labels.segments.wordCount(locate.data.segment.word_count)}
            </span>
            {locate.data.content_state === "stale" ? (
              <Badge variant="secondary" className="rounded-md font-normal">
                {labels.segments.locateStale}
              </Badge>
            ) : null}
            {onDismiss ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="ml-auto size-8 rounded-lg"
                aria-label={labels.segments.dismissLocate}
                onClick={onDismiss}
              >
                <XIcon aria-hidden className="size-4" />
              </Button>
            ) : null}
          </div>
          <p className="text-foreground/85 text-[13px] leading-6 [overflow-wrap:anywhere] whitespace-pre-wrap">
            {locate.data.segment.content}
          </p>
          <KnowledgeSummaryBlock summary={locate.data.summary} />
        </div>
      )}
    </aside>
  );
}

function SegmentContentField({
  content,
  onChange,
}: {
  content: string;
  onChange: (value: string) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  return (
    <label className="flex min-h-0 flex-1 flex-col gap-3 text-[13px]">
      <span className="text-muted-foreground text-xs font-medium">
        {labels.segments.contentLabel}
      </span>
      <Textarea
        className="[field-sizing:fixed] min-h-60 flex-1 resize-none rounded-none border-0 bg-transparent px-0 py-0 text-[13px] leading-6 shadow-none focus-visible:ring-0 md:text-[13px]"
        value={content}
        rows={8}
        required
        maxLength={MAX_SEGMENT_CONTENT_CHARS}
        placeholder={labels.segments.contentPlaceholder}
        onChange={(event) => onChange(event.target.value)}
      />
      <span className="text-muted-foreground text-right text-xs tabular-nums">
        {labels.segments.wordCount(content.length)} /{" "}
        {MAX_SEGMENT_CONTENT_CHARS.toLocaleString()}
      </span>
    </label>
  );
}

function AddSegmentSheet({
  scope,
  base,
  document,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const createSegment = useCreateKnowledgeSegment(scope);
  const [content, setContent] = useState("");

  return (
    <Sheet
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent
        overlayClassName="bg-slate-950/5 dark:bg-black/30"
        side="right"
        closeLabel={labels.segments.close}
        className="border-border/70 h-dvh w-full gap-0 overflow-hidden text-[13px] sm:max-w-[560px]"
      >
        <SheetHeader className="border-border/60 shrink-0 gap-1 border-b px-5 py-4 pr-14">
          <SheetTitle className="text-[15px]">
            {labels.segments.addTitle}
          </SheetTitle>
          <SheetDescription className="text-xs leading-5">
            {labels.segments.addDescription}
          </SheetDescription>
        </SheetHeader>
        <form
          className="flex min-h-0 flex-1 flex-col"
          onSubmit={(event) => {
            event.preventDefault();
            if (!content.trim()) return;
            createSegment.mutate(
              {
                documentId: document.id,
                baseId: base.id,
                content: content.trim(),
              },
              { onSuccess: () => onClose() },
            );
          }}
        >
          <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-4">
            <SegmentContentField content={content} onChange={setContent} />
            {createSegment.error ? (
              <p role="alert" className="text-destructive shrink-0 text-[13px]">
                {knowledgeErrorMessage(createSegment.error, labels.errors)}
              </p>
            ) : null}
          </div>
          <SheetFooter className="border-border/60 mt-0 shrink-0 flex-row justify-end border-t px-5 py-3">
            <Button
              type="button"
              variant="outline"
              className="bg-muted/70 h-8 rounded-md border-transparent text-[13px] shadow-none"
              onClick={onClose}
            >
              {labels.common.cancel}
            </Button>
            <Button
              type="submit"
              className="h-8 rounded-md bg-blue-600 text-[13px] text-white hover:bg-blue-700 focus-visible:ring-blue-300"
              disabled={createSegment.isPending || !content.trim()}
            >
              {createSegment.isPending
                ? labels.common.saving
                : labels.common.save}
            </Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}

function EditSegmentSheet({
  scope,
  base,
  document,
  segment,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem;
  segment: KnowledgeSegmentItem;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const updateSegment = useUpdateKnowledgeSegment(scope);
  const [content, setContent] = useState(segment.content);

  return (
    <Sheet
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent
        overlayClassName="bg-slate-950/5 dark:bg-black/30"
        side="right"
        closeLabel={labels.segments.close}
        className="border-border/70 h-dvh w-full gap-0 overflow-hidden text-[13px] sm:max-w-[560px]"
      >
        <SheetHeader className="border-border/60 shrink-0 gap-1 border-b px-5 py-4 pr-14">
          <SheetTitle className="text-[15px]">
            {labels.segments.editTitle(segment.position)}
          </SheetTitle>
          <SheetDescription className="truncate text-xs leading-5">
            {document.name} · {labels.segments.wordCount(segment.word_count)}
          </SheetDescription>
        </SheetHeader>
        <form
          className="flex min-h-0 flex-1 flex-col"
          onSubmit={(event) => {
            event.preventDefault();
            if (!content.trim()) return;
            updateSegment.mutate(
              {
                segmentId: segment.id,
                documentId: document.id,
                baseId: base.id,
                input: { content: content.trim() },
              },
              { onSuccess: () => onClose() },
            );
          }}
        >
          <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-4">
            <SegmentContentField content={content} onChange={setContent} />
            {updateSegment.error ? (
              <p role="alert" className="text-destructive shrink-0 text-[13px]">
                {knowledgeErrorMessage(updateSegment.error, labels.errors)}
              </p>
            ) : null}
          </div>
          <SheetFooter className="border-border/60 mt-0 shrink-0 flex-row justify-end border-t px-5 py-3">
            <Button
              type="button"
              variant="outline"
              className="bg-muted/70 h-8 rounded-md border-transparent text-[13px] shadow-none"
              onClick={onClose}
            >
              {labels.common.cancel}
            </Button>
            <Button
              type="submit"
              className="h-8 rounded-md bg-blue-600 text-[13px] text-white hover:bg-blue-700 focus-visible:ring-blue-300"
              disabled={updateSegment.isPending || !content.trim()}
            >
              {updateSegment.isPending
                ? labels.common.saving
                : labels.common.save}
            </Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}

function ViewSegmentSheet({
  scope,
  base,
  document,
  segment,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem;
  segment: KnowledgeSegmentItem;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const detail = useKnowledgeSegmentLocate(
    scope,
    base.id,
    document,
    segment.id,
  );

  return (
    <Sheet
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent
        overlayClassName="bg-slate-950/5 dark:bg-black/30"
        side="right"
        closeLabel={labels.segments.close}
        className="border-border/70 h-dvh w-full gap-0 overflow-hidden text-[13px] sm:max-w-[560px]"
      >
        <SheetHeader className="border-border/60 shrink-0 gap-1 border-b px-5 py-4 pr-14">
          <SheetTitle className="text-[15px]">
            {labels.segments.position(segment.position)}
          </SheetTitle>
          <SheetDescription className="truncate text-xs leading-5">
            {document.name} · {labels.segments.wordCount(segment.word_count)}
          </SheetDescription>
        </SheetHeader>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {detail.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {labels.segments.locateFailed}
            </p>
          ) : detail.data ? (
            <>
              <KnowledgeSummaryBlock summary={detail.data.summary} />
              <p className="text-[13px] leading-6 [overflow-wrap:anywhere] whitespace-pre-wrap">
                {detail.data.segment.content}
              </p>
            </>
          ) : (
            <Skeleton className="h-28" />
          )}
        </div>
        <SheetFooter className="border-border/60 mt-0 shrink-0 flex-row justify-end border-t px-5 py-3">
          <Button
            type="button"
            variant="outline"
            className="bg-muted/70 h-8 rounded-md border-transparent text-[13px] shadow-none"
            onClick={onClose}
          >
            {labels.segments.close}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}

function DeleteSegmentDialog({
  scope,
  base,
  document,
  segment,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem;
  segment: KnowledgeSegmentItem;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const deleteSegment = useDeleteKnowledgeSegment(scope);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="border-border/70 rounded-lg text-[13px]">
        <DialogHeader>
          <DialogTitle className="text-base tracking-tight">
            {labels.segments.deleteTitle}
          </DialogTitle>
          <DialogDescription className="text-xs leading-5">
            {labels.segments.deleteDescription(segment.position)}
          </DialogDescription>
        </DialogHeader>
        {deleteSegment.error ? (
          <p role="alert" className="text-destructive text-[13px]">
            {knowledgeErrorMessage(deleteSegment.error, labels.errors)}
          </p>
        ) : null}
        <DialogFooter className="border-border/60 border-t pt-4">
          <Button
            type="button"
            variant="outline"
            className="border-border/70 rounded-lg text-[13px] shadow-none"
            onClick={onClose}
          >
            {labels.common.cancel}
          </Button>
          <Button
            type="button"
            variant="destructive"
            className="rounded-lg text-[13px]"
            disabled={deleteSegment.isPending}
            onClick={() =>
              deleteSegment.mutate(
                {
                  segmentId: segment.id,
                  documentId: document.id,
                  baseId: base.id,
                },
                { onSuccess: () => onClose() },
              )
            }
          >
            {deleteSegment.isPending
              ? labels.common.deleting
              : labels.common.delete}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
