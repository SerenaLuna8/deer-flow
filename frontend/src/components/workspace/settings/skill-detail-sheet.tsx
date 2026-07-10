"use client";

import type { RefObject } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { MarkdownContent } from "@/components/workspace/messages/markdown-content";
import { useI18n } from "@/core/i18n/hooks";
import { SkillRequestError } from "@/core/skills/api";
import { useSkillContent } from "@/core/skills/hooks";
import { splitSkillMarkdown } from "@/core/skills/markdown";
import type { Skill } from "@/core/skills/type";

export function SkillDetailSheet({
  skill,
  open,
  onOpenChange,
  openerRef,
}: {
  skill: Skill | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  openerRef: RefObject<HTMLButtonElement | null>;
}) {
  const { t } = useI18n();
  const { data, isLoading, error, refetch } = useSkillContent(
    skill?.name ?? null,
    open,
  );
  const body = data ? splitSkillMarkdown(data.content).body : "";
  const errorMessage =
    error instanceof SkillRequestError && error.status === 403
      ? t.settings.skills.adminRequiredPreview
      : error instanceof SkillRequestError && error.status === 404
        ? t.settings.skills.contentUnavailable
        : t.settings.skills.loadError;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        className="w-full max-w-none gap-0 overflow-hidden p-0 sm:w-[min(92vw,800px)] sm:max-w-[800px]"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          if (openerRef.current?.isConnected) {
            openerRef.current.focus();
          }
        }}
      >
        <SheetHeader className="shrink-0 border-b px-5 py-4 pr-12 sm:px-6">
          <SheetTitle className="text-lg break-words">{skill?.name}</SheetTitle>
          <SheetDescription>
            {t.settings.skills.fileLabel}
            <span className="sr-only">
              {" "}
              · {t.settings.skills.renderedDescription}
            </span>
          </SheetDescription>
          <div className="flex flex-wrap items-center gap-2 pt-2">
            {skill && (
              <Badge variant="secondary">
                {t.settings.skills.categories[skill.category]}
              </Badge>
            )}
            {skill && (
              <Badge variant="secondary">
                {skill.enabled
                  ? t.settings.skills.enabled
                  : t.settings.skills.disabled}
              </Badge>
            )}
          </div>
          {skill?.description && (
            <p className="text-muted-foreground pt-2 text-sm leading-6">
              {skill.description}
            </p>
          )}
          {skill?.license && (
            <p className="text-muted-foreground text-sm">
              {t.settings.skills.licenseLabel}: {skill.license}
            </p>
          )}
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-6 sm:px-8">
          <div className="mx-auto w-full max-w-[72ch] min-w-0 [overflow-wrap:anywhere]">
            {isLoading ? (
              <SkillDetailSkeleton />
            ) : error ? (
              <div role="alert" className="space-y-3 text-sm">
                <p>{errorMessage}</p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void refetch()}
                >
                  {t.settings.skills.retry}
                </Button>
              </div>
            ) : body.trim() ? (
              <MarkdownContent content={body} isLoading={false} />
            ) : (
              <p className="text-muted-foreground text-sm">
                {t.settings.skills.emptyContent}
              </p>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function SkillDetailSkeleton() {
  const { t } = useI18n();

  return (
    <div
      role="status"
      aria-label={t.settings.skills.loading}
      className="space-y-4"
    >
      <span className="sr-only">{t.settings.skills.loading}</span>
      <Skeleton className="h-7 w-2/5" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-5/6" />
      <Skeleton className="h-28 w-full" />
    </div>
  );
}
