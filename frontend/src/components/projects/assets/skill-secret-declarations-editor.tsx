"use client";

import {
  AlertCircleIcon,
  Code2Icon,
  KeyRoundIcon,
  Loader2Icon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import {
  SharedAssetApiError,
  parseProjectSkillFrontmatter,
  patchProjectSkillFrontmatter,
  sha256SkillContent,
  skillSecretDeclarationNameSchema,
  type SkillFrontmatterDiagnostic,
  type SkillSecretProjection,
} from "@/core/shared-assets";

const PARSE_DEBOUNCE_MS = 250;

type EditorStatus =
  | { kind: "idle" | "parsing" | "patching" }
  | {
      kind: "ready";
      sourceSha256: string;
      projection: SkillSecretProjection;
      patchable: boolean;
      diagnostics: SkillFrontmatterDiagnostic[];
    }
  | {
      kind: "invalid";
      sourceSha256: string;
      diagnostics: SkillFrontmatterDiagnostic[];
    }
  | { kind: "error"; message: string };

export function skillFrontmatterResponseIsCurrent({
  generation,
  currentGeneration,
  sourceContent,
  currentContent,
  sourceSha256,
  responseSourceSha256,
}: {
  generation: number;
  currentGeneration: number;
  sourceContent: string;
  currentContent: string;
  sourceSha256: string;
  responseSourceSha256: string;
}): boolean {
  return (
    skillFrontmatterRequestIsCurrent({
      generation,
      currentGeneration,
      sourceContent,
      currentContent,
    }) && sourceSha256 === responseSourceSha256
  );
}

export function skillFrontmatterRequestIsCurrent({
  generation,
  currentGeneration,
  sourceContent,
  currentContent,
}: {
  generation: number;
  currentGeneration: number;
  sourceContent: string;
  currentContent: string;
}): boolean {
  return generation === currentGeneration && sourceContent === currentContent;
}

export function skillSecretDraftAfterPatch({
  patchSucceeded,
  name,
  optional,
}: {
  patchSucceeded: boolean;
  name: string;
  optional: boolean;
}): { name: string; optional: boolean } {
  return patchSucceeded ? { name: "", optional: false } : { name, optional };
}

function declarationErrorMessage(
  error: unknown,
  copy: Translations["skills"]["secrets"],
): string {
  if (error instanceof SharedAssetApiError) {
    if (error.code === "SKILL_FRONTMATTER_SOURCE_STALE") {
      return copy.sourceStale;
    }
    if (error.code === "SKILL_SECRET_DECLARATION_INVALID") {
      return copy.invalidDeclaration;
    }
    if (error.status === 403) return copy.forbidden;
    if (error.status === 404) return copy.notFound;
    if (error.code === "ASSET_RESPONSE_INVALID") {
      return copy.invalidResponse;
    }
  }
  return copy.unavailable;
}

function diagnosticLocation(
  diagnostic: SkillFrontmatterDiagnostic,
  copy: Translations["skills"]["secrets"],
): string {
  if (diagnostic.line === null) return "";
  return copy.location(diagnostic.line, diagnostic.column);
}

export function SkillSecretDeclarationsEditor({
  projectId,
  content,
  canEdit,
  disabled = false,
  onContentChange,
  onOpenSource,
  onValidityChange,
}: {
  projectId: string;
  content: string;
  canEdit: boolean;
  disabled?: boolean;
  onContentChange: (content: string) => void;
  onOpenSource: () => void;
  onValidityChange?: (valid: boolean) => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.secrets;
  const [status, setStatus] = useState<EditorStatus>({ kind: "idle" });
  const [newName, setNewName] = useState("");
  const [newOptional, setNewOptional] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [parseNonce, setParseNonce] = useState(0);
  const contentRef = useRef(content);
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  contentRef.current = content;

  useEffect(() => {
    const generation = ++generationRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setStatus({ kind: "parsing" });
    setLocalError(null);

    const timeout = window.setTimeout(() => {
      const sourceContent = content;
      void (async () => {
        try {
          const sourceSha256 = await sha256SkillContent(sourceContent);
          if (
            controller.signal.aborted ||
            !skillFrontmatterRequestIsCurrent({
              generation,
              currentGeneration: generationRef.current,
              sourceContent,
              currentContent: contentRef.current,
            })
          ) {
            return;
          }
          const response = await parseProjectSkillFrontmatter(
            projectId,
            { content: sourceContent, source_sha256: sourceSha256 },
            controller.signal,
          );
          if (
            controller.signal.aborted ||
            !skillFrontmatterResponseIsCurrent({
              generation,
              currentGeneration: generationRef.current,
              sourceContent,
              currentContent: contentRef.current,
              sourceSha256,
              responseSourceSha256: response.source_sha256,
            })
          ) {
            return;
          }
          setStatus(
            response.valid && response.projection
              ? {
                  kind: "ready",
                  sourceSha256,
                  projection: response.projection,
                  patchable: response.patchable,
                  diagnostics: response.diagnostics,
                }
              : {
                  kind: "invalid",
                  sourceSha256,
                  diagnostics: response.diagnostics,
                },
          );
        } catch (error) {
          if (
            controller.signal.aborted ||
            !skillFrontmatterRequestIsCurrent({
              generation,
              currentGeneration: generationRef.current,
              sourceContent,
              currentContent: contentRef.current,
            })
          ) {
            return;
          }
          setStatus({
            kind: "error",
            message: declarationErrorMessage(error, copy),
          });
        }
      })();
    }, PARSE_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [content, copy, parseNonce, projectId]);

  const valid = status.kind === "ready";
  useEffect(() => {
    onValidityChange?.(valid);
  }, [onValidityChange, valid]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      onValidityChange?.(false);
    },
    [onValidityChange],
  );

  async function patchProjection(
    requiredSecrets: SkillSecretProjection["required_secrets"],
    secretsAutonomous: boolean,
  ): Promise<boolean> {
    if (status.kind !== "ready" || !status.patchable || disabled || !canEdit) {
      return false;
    }
    const sourceContent = contentRef.current;
    const sourceSha256 = status.sourceSha256;
    const generation = ++generationRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setStatus({ kind: "patching" });
    setLocalError(null);
    try {
      const response = await patchProjectSkillFrontmatter(
        projectId,
        {
          content: sourceContent,
          source_sha256: sourceSha256,
          required_secrets: requiredSecrets,
          secrets_autonomous: secretsAutonomous,
        },
        controller.signal,
      );
      if (
        controller.signal.aborted ||
        !skillFrontmatterResponseIsCurrent({
          generation,
          currentGeneration: generationRef.current,
          sourceContent,
          currentContent: contentRef.current,
          sourceSha256,
          responseSourceSha256: response.source_sha256,
        })
      ) {
        return false;
      }
      contentRef.current = response.content;
      setStatus({
        kind: "ready",
        sourceSha256: response.result_sha256,
        projection: response.projection,
        patchable: true,
        diagnostics: response.diagnostics,
      });
      if (response.changed) onContentChange(response.content);
      return true;
    } catch (error) {
      if (
        controller.signal.aborted ||
        !skillFrontmatterRequestIsCurrent({
          generation,
          currentGeneration: generationRef.current,
          sourceContent,
          currentContent: contentRef.current,
        })
      ) {
        return false;
      }
      setStatus(
        error instanceof SharedAssetApiError &&
          error.code === "SKILL_SECRET_DECLARATION_INVALID" &&
          error.diagnostics
          ? {
              kind: "invalid",
              sourceSha256,
              diagnostics: [...error.diagnostics],
            }
          : {
              kind: "error",
              message: declarationErrorMessage(error, copy),
            },
      );
      return false;
    }
  }

  function addRequirement() {
    if (status.kind !== "ready") return;
    const name = newName.trim();
    if (!skillSecretDeclarationNameSchema.safeParse(name).success) {
      setLocalError(copy.invalidName);
      return;
    }
    if (
      status.projection.required_secrets.some(
        (requirement) => requirement.name === name,
      )
    ) {
      setLocalError(copy.duplicateName);
      return;
    }
    void patchProjection(
      [...status.projection.required_secrets, { name, optional: newOptional }],
      status.projection.secrets_autonomous,
    ).then((succeeded) => {
      const nextDraft = skillSecretDraftAfterPatch({
        patchSucceeded: succeeded,
        name: newName,
        optional: newOptional,
      });
      setNewName(nextDraft.name);
      setNewOptional(nextDraft.optional);
    });
  }

  const busy =
    status.kind === "idle" ||
    status.kind === "parsing" ||
    status.kind === "patching";
  const controlsDisabled = busy || disabled || !canEdit;
  const diagnostics =
    status.kind === "ready" || status.kind === "invalid"
      ? status.diagnostics
      : [];

  return (
    <section aria-label={copy.aria} aria-busy={busy} className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <KeyRoundIcon aria-hidden className="size-4" />
            <h3 className="text-sm font-semibold">{copy.title}</h3>
          </div>
          <p className="text-muted-foreground mt-1 max-w-2xl text-xs leading-5">
            {copy.description}
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onOpenSource}
        >
          <Code2Icon aria-hidden className="size-4" />
          {copy.viewSource}
        </Button>
      </div>

      {busy ? (
        <div
          role="status"
          aria-live="polite"
          className="text-muted-foreground flex items-center gap-2 rounded-lg border border-dashed p-4 text-sm"
        >
          <Loader2Icon aria-hidden className="size-4 animate-spin" />
          {status.kind === "patching" ? copy.syncing : copy.checking}
        </div>
      ) : null}

      {status.kind === "error" ? (
        <div className="border-destructive/30 space-y-3 rounded-lg border p-4">
          <p role="alert" className="text-destructive text-sm">
            {status.message}
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setParseNonce((current) => current + 1)}
          >
            <RefreshCwIcon aria-hidden className="size-4" />
            {copy.retry}
          </Button>
        </div>
      ) : null}

      {status.kind === "invalid" ? (
        <div className="border-destructive/30 space-y-3 rounded-lg border p-4">
          <div className="text-destructive flex items-start gap-2 text-sm">
            <AlertCircleIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
            <p role="alert">{copy.invalidSource}</p>
          </div>
          <Button type="button" size="sm" onClick={onOpenSource}>
            {copy.openSource}
          </Button>
        </div>
      ) : null}

      {status.kind === "ready" ? (
        <>
          {!status.patchable ? (
            <div className="border-warning/30 bg-warning/5 rounded-lg border p-4 text-sm">
              <p role="alert">{copy.managedComments}</p>
            </div>
          ) : null}

          {status.projection.shorthand_count > 0 ? (
            <p role="status" className="text-muted-foreground text-xs">
              {copy.shorthand(status.projection.shorthand_count)}
            </p>
          ) : null}

          <div className="space-y-3">
            {status.projection.required_secrets.length === 0 ? (
              <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
                {copy.empty}
              </p>
            ) : (
              status.projection.required_secrets.map((requirement) => (
                <div
                  key={requirement.name}
                  className="bg-muted/20 flex flex-col gap-3 rounded-lg border p-3 sm:flex-row sm:items-center"
                >
                  <code className="min-w-0 flex-1 text-sm font-medium break-all">
                    {requirement.name}
                  </code>
                  <Badge variant="secondary">
                    {requirement.optional ? copy.optional : copy.required}
                  </Badge>
                  <label className="flex items-center gap-2 text-xs">
                    <Switch
                      aria-label={copy.setOptional(requirement.name)}
                      checked={requirement.optional}
                      disabled={controlsDisabled || !status.patchable}
                      onCheckedChange={(checked) =>
                        void patchProjection(
                          status.projection.required_secrets.map((current) =>
                            current.name === requirement.name
                              ? { ...current, optional: checked }
                              : current,
                          ),
                          status.projection.secrets_autonomous,
                        )
                      }
                    />
                    {copy.optional}
                  </label>
                  {canEdit ? (
                    <Button
                      type="button"
                      size="icon-sm"
                      variant="ghost"
                      aria-label={copy.remove(requirement.name)}
                      disabled={controlsDisabled || !status.patchable}
                      onClick={() =>
                        void patchProjection(
                          status.projection.required_secrets.filter(
                            (current) => current.name !== requirement.name,
                          ),
                          status.projection.secrets_autonomous,
                        )
                      }
                    >
                      <Trash2Icon aria-hidden className="size-4" />
                    </Button>
                  ) : null}
                </div>
              ))
            )}
          </div>

          {canEdit ? (
            <div className="space-y-3 rounded-lg border border-dashed p-3">
              <p className="text-sm font-medium">{copy.addTitle}</p>
              <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-end">
                <label className="space-y-1">
                  <span className="text-muted-foreground text-xs">
                    {copy.nameLabel}
                  </span>
                  <Input
                    value={newName}
                    placeholder={copy.namePlaceholder}
                    disabled={controlsDisabled || !status.patchable}
                    aria-invalid={Boolean(localError)}
                    aria-describedby={
                      localError ? "skill-secret-name-error" : undefined
                    }
                    onChange={(event) => {
                      setNewName(event.target.value);
                      setLocalError(null);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addRequirement();
                      }
                    }}
                  />
                </label>
                <label className="flex h-9 items-center gap-2 text-xs">
                  <Switch
                    aria-label={copy.newOptional}
                    checked={newOptional}
                    disabled={controlsDisabled || !status.patchable}
                    onCheckedChange={setNewOptional}
                  />
                  {copy.optional}
                </label>
                <Button
                  type="button"
                  variant="outline"
                  disabled={
                    controlsDisabled ||
                    !status.patchable ||
                    newName.trim() === ""
                  }
                  onClick={addRequirement}
                >
                  <PlusIcon aria-hidden className="size-4" />
                  {copy.add}
                </Button>
              </div>
              {localError ? (
                <p
                  id="skill-secret-name-error"
                  role="alert"
                  className="text-destructive text-xs"
                >
                  {localError}
                </p>
              ) : null}
            </div>
          ) : null}

          <div className="flex items-start justify-between gap-4 rounded-lg border p-3">
            <div>
              <p className="text-sm font-medium">{copy.autonomousTitle}</p>
              <p className="text-muted-foreground mt-1 text-xs leading-5">
                {copy.autonomousDescription}
              </p>
            </div>
            <Switch
              aria-label={copy.autonomousAria}
              checked={status.projection.secrets_autonomous}
              disabled={controlsDisabled || !status.patchable}
              onCheckedChange={(checked) =>
                void patchProjection(
                  status.projection.required_secrets,
                  checked,
                )
              }
            />
          </div>
        </>
      ) : null}

      {diagnostics.length > 0 ? (
        <ul aria-live="polite" className="space-y-1 text-xs">
          {diagnostics.map((diagnostic, index) => (
            <li
              key={`${diagnostic.code}:${diagnostic.line ?? ""}:${index}`}
              className={
                diagnostic.severity === "error"
                  ? "text-destructive"
                  : "text-muted-foreground"
              }
            >
              {diagnosticLocation(diagnostic, copy)}
              {diagnostic.public_message}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
