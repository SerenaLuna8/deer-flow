"use client";

import {
  AlertCircleIcon,
  CheckCircle2Icon,
  Code2Icon,
  KeyRoundIcon,
  Loader2Icon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
} from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";

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
      feedback: SkillSecretFeedbackKind;
    }
  | {
      kind: "invalid";
      sourceSha256: string;
      diagnostics: SkillFrontmatterDiagnostic[];
    }
  | { kind: "error"; message: string };

type SkillSecretFeedbackKind = "source" | "draft";
export type SkillSecretInjectionMode = "automatic" | "explicit";

export function resolveSkillSecretEditorAccess({
  editable,
  canEdit,
  canBeginEdit,
}: {
  editable?: boolean;
  canEdit?: boolean;
  canBeginEdit?: boolean;
}): { editable: boolean; canBeginEdit: boolean } {
  const resolvedEditable = editable ?? canEdit ?? false;
  return {
    editable: resolvedEditable,
    canBeginEdit: !resolvedEditable && Boolean(canBeginEdit),
  };
}

export function skillSecretFeedbackMessage(
  copy: Translations["skills"]["secrets"],
  feedback: SkillSecretFeedbackKind,
  count: number,
): string {
  return feedback === "draft" ? copy.draftUpdated : copy.recognized(count);
}

export function skillSecretInjectionModeFromAutonomous(
  autonomous: boolean,
): SkillSecretInjectionMode {
  return autonomous ? "automatic" : "explicit";
}

export function skillSecretAutonomousFromInjectionMode(
  mode: SkillSecretInjectionMode,
): boolean {
  return mode === "automatic";
}

export function shouldShowSkillSecretInjectionSettings(
  _requiredSecretCount: number,
): boolean {
  // `secrets-autonomous` remains a supported SKILL.md/runtime contract. It is
  // intentionally not exposed through this page.
  return false;
}

export function skillSecretNameFocusDecision({
  editable,
  focusRequested,
  inputReady,
}: {
  editable: boolean;
  focusRequested: boolean;
  inputReady: boolean;
}): { shouldFocus: boolean; keepRequest: boolean } {
  if (!focusRequested) {
    return { shouldFocus: false, keepRequest: false };
  }
  if (!editable || !inputReady) {
    return { shouldFocus: false, keepRequest: true };
  }
  return { shouldFocus: true, keepRequest: false };
}

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
  targetEnv,
  optional,
}: {
  patchSucceeded: boolean;
  name: string;
  targetEnv: string;
  optional: boolean;
}): { name: string; targetEnv: string; optional: boolean } {
  return patchSucceeded
    ? { name: "", targetEnv: "", optional: false }
    : { name, targetEnv, optional };
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
  editable,
  canBeginEdit = false,
  readOnlyReason,
  disabled = false,
  beforeAdvancedSettings,
  onContentChange,
  onBeginEdit,
  onOpenSource,
  onValidityChange,
}: {
  projectId: string;
  content: string;
  /** @deprecated Prefer `editable`; retained while callers migrate. */
  canEdit?: boolean;
  editable?: boolean;
  canBeginEdit?: boolean;
  readOnlyReason?: string;
  disabled?: boolean;
  beforeAdvancedSettings?: ReactNode;
  onContentChange: (content: string) => void;
  onBeginEdit?: () => void;
  onOpenSource: () => void;
  onValidityChange?: (valid: boolean) => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.secrets;
  const [status, setStatus] = useState<EditorStatus>({ kind: "idle" });
  const [newName, setNewName] = useState("");
  const [newTargetEnv, setNewTargetEnv] = useState("");
  const [newOptional, setNewOptional] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [parseNonce, setParseNonce] = useState(0);
  const injectionModeName = useId();
  const contentRef = useRef(content);
  const lastPatchedContentRef = useRef<string | null>(null);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const focusNameAfterBeginEditRef = useRef(false);
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  contentRef.current = content;
  const access = resolveSkillSecretEditorAccess({
    editable,
    canEdit,
    canBeginEdit,
  });

  useEffect(() => {
    const decision = skillSecretNameFocusDecision({
      editable: access.editable,
      focusRequested: focusNameAfterBeginEditRef.current,
      inputReady: status.kind === "ready",
    });
    if (!decision.shouldFocus) {
      focusNameAfterBeginEditRef.current = decision.keepRequest;
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      const input = nameInputRef.current;
      if (!input) {
        focusNameAfterBeginEditRef.current = true;
        return;
      }
      focusNameAfterBeginEditRef.current = false;
      input.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [access.editable, status.kind]);

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
                  feedback:
                    sourceContent === lastPatchedContentRef.current
                      ? "draft"
                      : "source",
                }
              : {
                  kind: "invalid",
                  sourceSha256,
                  diagnostics: response.diagnostics,
                },
          );
          if (sourceContent !== lastPatchedContentRef.current) {
            lastPatchedContentRef.current = null;
          }
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
    if (
      status.kind !== "ready" ||
      !status.patchable ||
      disabled ||
      !access.editable
    ) {
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
      lastPatchedContentRef.current = response.content;
      setStatus({
        kind: "ready",
        sourceSha256: response.result_sha256,
        projection: response.projection,
        patchable: true,
        diagnostics: response.diagnostics,
        feedback: "draft",
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
    const targetEnv = newTargetEnv.trim();
    if (!skillSecretDeclarationNameSchema.safeParse(name).success) {
      setLocalError(copy.invalidName);
      return;
    }
    if (!skillSecretDeclarationNameSchema.safeParse(targetEnv).success) {
      setLocalError(copy.invalidTargetEnv);
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
    if (
      status.projection.required_secrets.some(
        (requirement) => requirement.target_env === targetEnv,
      )
    ) {
      setLocalError(copy.duplicateTargetEnv);
      return;
    }
    void patchProjection(
      [
        ...status.projection.required_secrets,
        { name, target_env: targetEnv, optional: newOptional },
      ],
      status.projection.secrets_autonomous,
    ).then((succeeded) => {
      const nextDraft = skillSecretDraftAfterPatch({
        patchSucceeded: succeeded,
        name: newName,
        targetEnv: newTargetEnv,
        optional: newOptional,
      });
      setNewName(nextDraft.name);
      setNewTargetEnv(nextDraft.targetEnv);
      setNewOptional(nextDraft.optional);
    });
  }

  const busy =
    status.kind === "idle" ||
    status.kind === "parsing" ||
    status.kind === "patching";
  const controlsDisabled = busy || disabled || !access.editable;
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

          <p
            role="status"
            aria-live="polite"
            className="text-muted-foreground flex items-center gap-2 text-xs"
          >
            <CheckCircle2Icon aria-hidden className="size-3.5" />
            {skillSecretFeedbackMessage(
              copy,
              status.feedback,
              status.projection.required_secrets.length,
            )}
          </p>

          <div className="space-y-3">
            {status.projection.required_secrets.length === 0 ? (
              <div className="space-y-3 rounded-lg border border-dashed p-4">
                {access.editable ? (
                  <p className="text-muted-foreground text-sm">{copy.empty}</p>
                ) : null}
                {access.canBeginEdit && onBeginEdit ? (
                  <Button
                    type="button"
                    size="sm"
                    disabled={disabled}
                    onClick={() => {
                      focusNameAfterBeginEditRef.current = true;
                      onBeginEdit();
                    }}
                  >
                    <PlusIcon aria-hidden className="size-4" />
                    {copy.beginEdit}
                  </Button>
                ) : null}
                {!access.editable && readOnlyReason ? (
                  <p className="text-muted-foreground text-xs leading-5">
                    {readOnlyReason}
                  </p>
                ) : null}
              </div>
            ) : (
              status.projection.required_secrets.map((requirement) => (
                <div
                  key={requirement.name}
                  className="bg-muted/20 flex flex-col gap-3 rounded-lg border p-3 sm:flex-row sm:items-center"
                >
                  <code className="min-w-0 text-sm font-medium break-all">
                    {requirement.name}
                  </code>
                  <span aria-hidden className="text-muted-foreground">
                    →
                  </span>
                  <code className="min-w-0 flex-1 text-sm break-all">
                    {requirement.target_env}
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
                  {access.editable ? (
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

          {access.editable ? (
            <div className="space-y-3 rounded-lg border border-dashed p-3">
              <p className="text-sm font-medium">{copy.addTitle}</p>
              <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto_auto] sm:items-end">
                <label className="space-y-1">
                  <span className="text-muted-foreground text-xs">
                    {copy.nameLabel}
                  </span>
                  <Input
                    ref={nameInputRef}
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
                <label className="space-y-1">
                  <span className="text-muted-foreground text-xs">
                    {copy.targetEnvLabel}
                  </span>
                  <Input
                    value={newTargetEnv}
                    placeholder={copy.targetEnvPlaceholder}
                    disabled={controlsDisabled || !status.patchable}
                    aria-invalid={Boolean(localError)}
                    aria-describedby={
                      localError ? "skill-secret-name-error" : undefined
                    }
                    onChange={(event) => {
                      setNewTargetEnv(event.target.value);
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
                    newName.trim() === "" ||
                    newTargetEnv.trim() === ""
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

          {!access.editable &&
          status.projection.required_secrets.length > 0 &&
          readOnlyReason ? (
            <p className="text-muted-foreground text-xs leading-5">
              {readOnlyReason}
            </p>
          ) : null}
        </>
      ) : null}

      {beforeAdvancedSettings}

      {status.kind === "ready" &&
      shouldShowSkillSecretInjectionSettings(
        status.projection.required_secrets.length,
      ) ? (
        <details className="group rounded-lg border px-3 py-2">
          <summary className="cursor-pointer text-sm font-medium">
            {copy.advancedSettings}
          </summary>
          <fieldset className="mt-3 space-y-3 border-t pt-3">
            <legend className="text-sm font-medium">
              {copy.autonomousTitle}
            </legend>
            <p className="text-muted-foreground text-xs leading-5">
              {copy.autonomousDescription}
            </p>
            <div
              role="radiogroup"
              aria-label={copy.autonomousAria}
              className="grid gap-2 sm:grid-cols-2"
            >
              {(
                [
                  {
                    mode: "automatic" as const,
                    title: copy.injectionAutomatic,
                    description: copy.injectionAutomaticDescription,
                  },
                  {
                    mode: "explicit" as const,
                    title: copy.injectionExplicit,
                    description: copy.injectionExplicitDescription,
                  },
                ] satisfies Array<{
                  mode: SkillSecretInjectionMode;
                  title: string;
                  description: string;
                }>
              ).map((option) => {
                const checked =
                  skillSecretInjectionModeFromAutonomous(
                    status.projection.secrets_autonomous,
                  ) === option.mode;
                return (
                  <label
                    key={option.mode}
                    className={`flex items-start gap-3 rounded-lg border p-3 ${
                      checked ? "border-primary bg-primary/5" : "border-border"
                    } ${controlsDisabled ? "cursor-not-allowed" : "cursor-pointer"}`}
                  >
                    <input
                      type="radio"
                      name={injectionModeName}
                      value={option.mode}
                      checked={checked}
                      disabled={controlsDisabled || !status.patchable}
                      className="accent-primary mt-1"
                      onChange={() =>
                        void patchProjection(
                          status.projection.required_secrets,
                          skillSecretAutonomousFromInjectionMode(option.mode),
                        )
                      }
                    />
                    <span>
                      <span className="block text-sm font-medium">
                        {option.title}
                      </span>
                      <span className="text-muted-foreground mt-1 block text-xs leading-5">
                        {option.description}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>
        </details>
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
