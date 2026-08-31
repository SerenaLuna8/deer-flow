"use client";

import {
  ArrowLeftIcon,
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
      className="space-y-4"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <Button type="button" variant="ghost" size="sm" onClick={onBack}>
            <ArrowLeftIcon aria-hidden className="size-4" />
            {labels.detail.documents}
          </Button>
          <h2 className="mt-1 truncate text-lg font-semibold">
            {document.name}
          </h2>
          <p className="text-muted-foreground text-sm">
            {labels.segments.stats(document.segment_count, document.word_count)}
          </p>
        </div>
        {canEdit && document.status === "ready" ? (
          <Button type="button" onClick={() => setAdding(true)}>
            <PlusIcon aria-hidden className="size-4" />
            {labels.segments.add}
          </Button>
        ) : null}
      </div>

      {toggleSegment.error ? (
        <p role="alert" className="text-destructive text-sm">
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
        <Skeleton className="h-40 rounded-xl" />
      ) : segments.error ? (
        <p role="alert" className="text-destructive text-sm">
          {knowledgeErrorMessage(segments.error, labels.errors)}
        </p>
      ) : (segments.data?.items.length ?? 0) === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-sm">
          {labels.segments.empty}
        </p>
      ) : (
        <ol className="grid gap-3" data-testid="knowledge-segment-list">
          {segments.data?.items.map((segment) => {
            const position = formatKnowledgeSourcePosition(
              segment.source_position,
              labels.sourcePosition,
            );
            const manual = segment.source_position.manual === true;
            return (
              <li
                key={segment.id}
                className="border-border rounded-lg border p-3"
              >
                <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                  <span>{labels.segments.position(segment.position)}</span>
                  {position ? <span>· {position}</span> : null}
                  <span>· {labels.segments.wordCount(segment.word_count)}</span>
                  {manual ? (
                    <Badge variant="outline">
                      {labels.segments.manualBadge}
                    </Badge>
                  ) : null}
                  {!segment.enabled ? (
                    <Badge variant="secondary">{labels.status.disabled}</Badge>
                  ) : null}
                  <div className="ml-auto flex items-center gap-1.5">
                    {canEdit ? (
                      <>
                        <Switch
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
                          onClick={() => setEditing(segment)}
                        >
                          <PencilIcon aria-hidden className="size-4" />
                          {labels.segments.edit}
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="text-destructive"
                          onClick={() => setDeleting(segment)}
                        >
                          <Trash2Icon aria-hidden className="size-4" />
                          {labels.segments.delete}
                        </Button>
                      </>
                    ) : null}
                  </div>
                </div>
                <p
                  className={cn(
                    "mt-2 text-sm leading-6 whitespace-pre-wrap",
                    segment.enabled
                      ? "text-foreground"
                      : "text-muted-foreground",
                  )}
                >
                  {segment.content}
                </p>
              </li>
            );
          })}
        </ol>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={page <= 1 || segments.isFetching}
            onClick={() => setPage((current) => Math.max(1, current - 1))}
          >
            {labels.segments.previousPage}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={page >= pageCount || segments.isFetching}
            onClick={() => setPage((current) => current + 1)}
          >
            {labels.segments.nextPage}
          </Button>
        </div>
        {segments.data ? (
          <p className="text-muted-foreground text-sm">
            {labels.segments.pageInfo(page, pageCount, total)}
          </p>
        ) : null}
      </div>

      {adding ? (
        <AddSegmentDialog
          scope={scope}
          base={base}
          document={document}
          onClose={() => setAdding(false)}
        />
      ) : null}

      {editing ? (
        <EditSegmentDialog
          scope={scope}
          base={base}
          document={document}
          segment={editing}
          onClose={() => setEditing(null)}
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
  const locate = useKnowledgeSegmentLocate(
    scope,
    base.id,
    document.id,
    segmentId,
  );

  return (
    <aside
      aria-label={labels.segments.locatedTitle(
        locate.data?.segment.position ?? 0,
      )}
      data-testid="knowledge-segment-locate"
      className="border-primary/40 bg-primary/5 rounded-xl border p-3"
    >
      {locate.isLoading ? (
        <Skeleton className="h-16 rounded-lg" />
      ) : locate.error !== null || locate.data === undefined ? (
        <div className="flex items-start gap-2">
          <p role="alert" className="text-destructive min-w-0 flex-1 text-sm">
            {labels.segments.locateFailed}
          </p>
          {onDismiss ? (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7"
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
            <LocateIcon aria-hidden className="size-3.5" />
            <span className="text-foreground font-medium">
              {labels.segments.locatedTitle(locate.data.segment.position)}
            </span>
            <span>
              · {labels.segments.wordCount(locate.data.segment.word_count)}
            </span>
            {locate.data.content_state === "stale" ? (
              <Badge variant="secondary">{labels.segments.locateStale}</Badge>
            ) : null}
            {onDismiss ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="ml-auto size-7"
                aria-label={labels.segments.dismissLocate}
                onClick={onDismiss}
              >
                <XIcon aria-hidden className="size-4" />
              </Button>
            ) : null}
          </div>
          <p className="text-sm leading-6 whitespace-pre-wrap">
            {locate.data.segment.content}
          </p>
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
    <label className="grid gap-1.5 text-sm">
      <span className="font-medium">{labels.segments.contentLabel}</span>
      <Textarea
        value={content}
        rows={8}
        required
        maxLength={MAX_SEGMENT_CONTENT_CHARS}
        placeholder={labels.segments.contentPlaceholder}
        onChange={(event) => onChange(event.target.value)}
      />
      <span className="text-muted-foreground text-xs">
        {labels.segments.wordCount(content.length)}
      </span>
    </label>
  );
}

function AddSegmentDialog({
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
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{labels.segments.addTitle}</DialogTitle>
          <DialogDescription>
            {labels.segments.addDescription}
          </DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
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
          <SegmentContentField content={content} onChange={setContent} />
          {createSegment.error ? (
            <p role="alert" className="text-destructive text-sm">
              {knowledgeErrorMessage(createSegment.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {labels.common.cancel}
            </Button>
            <Button
              type="submit"
              disabled={createSegment.isPending || !content.trim()}
            >
              {createSegment.isPending
                ? labels.common.saving
                : labels.common.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function EditSegmentDialog({
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
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>
            {labels.segments.editTitle(segment.position)}
          </DialogTitle>
        </DialogHeader>
        <form
          className="grid gap-4"
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
          <SegmentContentField content={content} onChange={setContent} />
          {updateSegment.error ? (
            <p role="alert" className="text-destructive text-sm">
              {knowledgeErrorMessage(updateSegment.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {labels.common.cancel}
            </Button>
            <Button
              type="submit"
              disabled={updateSegment.isPending || !content.trim()}
            >
              {updateSegment.isPending
                ? labels.common.saving
                : labels.common.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
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
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{labels.segments.deleteTitle}</DialogTitle>
          <DialogDescription>
            {labels.segments.deleteDescription(segment.position)}
          </DialogDescription>
        </DialogHeader>
        {deleteSegment.error ? (
          <p role="alert" className="text-destructive text-sm">
            {knowledgeErrorMessage(deleteSegment.error, labels.errors)}
          </p>
        ) : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {labels.common.cancel}
          </Button>
          <Button
            type="button"
            variant="destructive"
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
