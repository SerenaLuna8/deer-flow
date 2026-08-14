"use client";

import { Loader2Icon, MessagesSquareIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import type { Project } from "@/core/projects/types";
import type { ProjectAssetItem } from "@/core/shared-assets";
import {
  SkillBuilderApiError,
  createSkillBuilderIdempotencyRegistry,
  skillBuilderCanAuthor,
  skillBuilderSemanticSignature,
  skillBuilderSessionPath,
  useCreateSkillBuilderRevisionSession,
} from "@/core/skill-builder";

import { skillBuilderErrorMessage } from "./skill-builder-start";

export function skillRevisionEntryVisible(
  capabilities: Project["capabilities"],
  item: Pick<
    ProjectAssetItem,
    "scope" | "status" | "current_published_version_id"
  >,
): boolean {
  return (
    item.scope === "project" &&
    item.current_published_version_id !== null &&
    (item.status === "active" || item.status === "suspended") &&
    skillBuilderCanAuthor(capabilities)
  );
}

export function skillRevisionEntryErrorMessage(
  error: unknown,
  copy: Translations["skills"]["builder"]["errors"],
): string {
  if (error instanceof SkillBuilderApiError) {
    if (error.serverCode === "SKILL_DESIGN_TARGET_SESSION_EXISTS") {
      return copy.targetSessionExists;
    }
    if (error.serverCode === "SKILL_DESIGN_TARGET_UNSUPPORTED") {
      return copy.targetUnsupported;
    }
    if (error.code === "SKILL_BUILDER_CONFLICT") {
      return copy.targetConflict;
    }
  }
  return skillBuilderErrorMessage(error, copy);
}

export function SkillRevisionEntry({
  project,
  item,
  disabled = false,
}: {
  project: Pick<Project, "id" | "slug" | "capabilities">;
  item: ProjectAssetItem;
  disabled?: boolean;
}) {
  const { user } = useAuth();
  const { t } = useI18n();
  const router = useRouter();
  const create = useCreateSkillBuilderRevisionSession(
    user?.id ?? "",
    project.id,
  );
  const [idempotency] = useState(() => createSkillBuilderIdempotencyRegistry());
  const [navigating, setNavigating] = useState(false);
  const copy = t.skills.builder.revision;

  if (!user || !skillRevisionEntryVisible(project.capabilities, item)) {
    return null;
  }

  const pending = create.isPending || navigating;

  function startRevision() {
    if (pending) return;
    const signature = skillBuilderSemanticSignature({
      kind: "revise",
      skill_id: item.id,
    });
    const command = idempotency.acquire("create", signature, (key) => ({
      kind: "revise" as const,
      skill_id: item.id,
      idempotency_key: key,
    }));
    create.mutate(command, {
      onSuccess: (response) => {
        idempotency.complete("create", signature);
        setNavigating(true);
        router.push(skillBuilderSessionPath(project.slug, response.data.id));
      },
    });
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled || pending}
          title={disabled ? copy.saveLocalChangesFirst : undefined}
          onClick={startRevision}
        >
          {pending ? (
            <Loader2Icon aria-hidden className="size-4 animate-spin" />
          ) : (
            <MessagesSquareIcon aria-hidden className="size-4" />
          )}
          {pending ? copy.opening : copy.button}
        </Button>
        <p className="text-muted-foreground text-xs">{copy.hint}</p>
      </div>
      {create.error ? (
        <p role="alert" className="text-destructive text-sm">
          {skillRevisionEntryErrorMessage(
            create.error,
            t.skills.builder.errors,
          )}
        </p>
      ) : null}
    </div>
  );
}
