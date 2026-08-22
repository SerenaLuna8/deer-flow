"use client";

import {
  BotIcon,
  FileTextIcon,
  LayoutDashboardIcon,
  Loader2Icon,
  WrenchIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useState, type KeyboardEvent } from "react";

import {
  AgentInstructionWorkspace,
  type AgentInstructionField,
} from "@/components/projects/assets/agent-instructions-workbench";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  agentBuilderBlueprintValidationIssue,
  type AgentBuilderBlueprint,
  type AgentBuilderConflict,
  type AgentBuilderConflictField,
} from "@/core/agent-builder";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import { resolveModelDisplayName } from "@/core/models/presentation";
import type { Model } from "@/core/models/types";

export function agentBuilderBlueprintValidationMessage(
  blueprint: AgentBuilderBlueprint,
  copy: Translations["agents"]["builder"]["blueprint"]["validation"],
): string | null {
  const issue = agentBuilderBlueprintValidationIssue(blueprint);
  switch (issue?.code) {
    case "description-required":
      return copy.descriptionRequired;
    case "model-required":
      return copy.modelRequired;
    case "tool-group-required":
      return copy.toolGroupRequired;
    case "document-required":
      return copy.documentRequired(issue.document);
    default:
      return null;
  }
}

const CONFLICT_FIELD_FILES: Record<AgentBuilderConflictField, string> = {
  agents_instructions: "AGENTS.md",
  soul: "SOUL.md",
  identity: "IDENTITY.md",
  user_context: "USER.md",
};

type AgentBuilderBlueprintSurface = "overview" | "documents";

export function agentBuilderBlueprintTabForKey(
  current: AgentBuilderBlueprintSurface,
  key: string,
): AgentBuilderBlueprintSurface | null {
  if (key === "Home") return "overview";
  if (key === "End") return "documents";
  if (key === "ArrowLeft" || key === "ArrowRight") {
    return current === "overview" ? "documents" : "overview";
  }
  return null;
}

export function AgentBuilderBlueprintReview({
  blueprint,
  agentName,
  agentSlug,
  agentSlugError,
  models,
  canAuthor,
  editing,
  pending,
  creating,
  dirty,
  canCreate,
  assumptions = [],
  conflicts = [],
  modelsLoading = false,
  modelsError = null,
  mcpDependencyLoading = false,
  mcpDependencyBlockReason = null,
  selectedField,
  displayMode,
  errorMessage,
  onSelectedFieldChange,
  onDisplayModeChange,
  onBlueprintChange,
  onAgentNameChange,
  onEdit,
  onSave,
  onDiscard,
  onCreate,
  onClose,
}: {
  blueprint: AgentBuilderBlueprint;
  agentName: string;
  agentSlug: string;
  agentSlugError: string | null;
  models: readonly Model[];
  canAuthor: boolean;
  editing: boolean;
  pending: boolean;
  creating: boolean;
  dirty: boolean;
  canCreate: boolean;
  assumptions?: readonly string[];
  conflicts?: readonly AgentBuilderConflict[];
  modelsLoading?: boolean;
  modelsError?: unknown;
  mcpDependencyLoading?: boolean;
  mcpDependencyBlockReason?: string | null;
  selectedField: AgentInstructionField;
  displayMode: "source" | "preview";
  errorMessage: string | null;
  onSelectedFieldChange: (field: AgentInstructionField) => void;
  onDisplayModeChange: (mode: "source" | "preview") => void;
  onBlueprintChange: (blueprint: AgentBuilderBlueprint) => void;
  onAgentNameChange: (value: string) => void;
  onEdit: () => void;
  onSave: () => void;
  onDiscard: () => void;
  onCreate: () => void;
  onClose?: () => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.builder.blueprint;
  const blueprintError = agentBuilderBlueprintValidationMessage(
    blueprint,
    copy.validation,
  );
  const resolvedModelDisplayName = resolveModelDisplayName(
    blueprint.model_ref,
    models,
  );
  const modelAvailable =
    !modelsLoading && !modelsError && Boolean(resolvedModelDisplayName);
  const modelDisplayName =
    resolvedModelDisplayName ?? t.conversation.agentModelUnavailableTitle;
  const effectiveEditing = editing && canAuthor;
  const showNormalizedName =
    Boolean(agentSlug) && agentName.trim() !== agentSlug;
  const hasBlockingConflict = conflicts.some(
    (conflict) => conflict.severity === "error",
  );
  const [surface, setSurface] =
    useState<AgentBuilderBlueprintSurface>("overview");

  useEffect(() => {
    if (effectiveEditing) setSurface("documents");
  }, [effectiveEditing]);

  function handleSurfaceKeyDown(
    current: AgentBuilderBlueprintSurface,
    event: KeyboardEvent<HTMLButtonElement>,
  ) {
    const next = agentBuilderBlueprintTabForKey(current, event.key);
    if (!next || (effectiveEditing && next === "overview")) return;
    event.preventDefault();
    setSurface(next);
    event.currentTarget.ownerDocument
      .getElementById(
        next === "overview"
          ? "agent-blueprint-overview-tab"
          : "agent-blueprint-documents-tab",
      )
      ?.focus();
  }

  return (
    <section
      data-testid="agent-builder-blueprint-panel"
      className="flex h-full min-h-0 flex-col"
      aria-label={copy.title}
    >
      <div className="border-border/70 flex min-h-14 shrink-0 items-center justify-between gap-3 border-b px-4">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">{copy.title}</h2>
          <p className="text-muted-foreground truncate text-xs">
            {copy.panelSummary(conflicts.length)}
          </p>
        </div>
        {onClose ? (
          <Button
            type="button"
            size="icon"
            variant="ghost"
            aria-label={copy.closeAria}
            onClick={onClose}
          >
            <XIcon aria-hidden />
          </Button>
        ) : null}
      </div>

      <div
        className="border-border/70 bg-muted/10 flex shrink-0 gap-1 border-b p-2"
        role="tablist"
        aria-label={copy.tabsAria}
      >
        <Button
          id="agent-blueprint-overview-tab"
          type="button"
          size="sm"
          variant={surface === "overview" ? "default" : "ghost"}
          role="tab"
          aria-selected={surface === "overview"}
          aria-controls="agent-blueprint-overview-panel"
          tabIndex={surface === "overview" ? 0 : -1}
          disabled={effectiveEditing}
          onClick={() => setSurface("overview")}
          onKeyDown={(event) => handleSurfaceKeyDown("overview", event)}
        >
          <LayoutDashboardIcon aria-hidden className="size-4" />
          {copy.overviewTab}
        </Button>
        <Button
          id="agent-blueprint-documents-tab"
          type="button"
          size="sm"
          variant={surface === "documents" ? "default" : "ghost"}
          role="tab"
          aria-selected={surface === "documents"}
          aria-controls="agent-blueprint-documents-panel"
          tabIndex={surface === "documents" ? 0 : -1}
          onClick={() => setSurface("documents")}
          onKeyDown={(event) => handleSurfaceKeyDown("documents", event)}
        >
          <FileTextIcon aria-hidden className="size-4" />
          {copy.documentsTab}
        </Button>
      </div>

      <div
        id="agent-blueprint-overview-panel"
        role="tabpanel"
        aria-labelledby="agent-blueprint-overview-tab"
        hidden={surface !== "overview"}
        className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5"
      >
        <div className="space-y-5">
          <section
            aria-labelledby="agent-runtime-title"
            className="border-border/70 grid gap-4 rounded-2xl border p-4 sm:grid-cols-2"
          >
            <div className="sm:col-span-2">
              <h3
                id="agent-runtime-title"
                className="flex items-center gap-2 text-sm font-semibold"
              >
                <BotIcon aria-hidden className="size-4" />
                {copy.runtime}
              </h3>
              <p className="text-muted-foreground mt-2 text-sm leading-6">
                {blueprint.description || copy.noDescription}
              </p>
            </div>
            <div className="bg-muted/25 rounded-xl p-3 sm:col-span-2">
              <label
                htmlFor="agent-builder-commit-name"
                className="text-muted-foreground text-xs"
              >
                {copy.nameLabel}
              </label>
              <Input
                id="agent-builder-commit-name"
                autoCapitalize="none"
                autoComplete="off"
                autoCorrect="off"
                spellCheck={false}
                value={agentName}
                disabled={!canAuthor || pending}
                aria-invalid={Boolean(agentSlugError)}
                aria-describedby={
                  agentSlugError
                    ? showNormalizedName
                      ? "agent-builder-commit-name-help agent-builder-commit-name-error"
                      : "agent-builder-commit-name-error"
                    : showNormalizedName
                      ? "agent-builder-commit-name-help"
                      : undefined
                }
                className="bg-background mt-2 h-11"
                onChange={(event) => onAgentNameChange(event.target.value)}
              />
              {showNormalizedName ? (
                <p
                  id="agent-builder-commit-name-help"
                  className="text-muted-foreground mt-2 text-xs leading-5"
                >
                  {copy.savedAs(agentSlug)}
                </p>
              ) : null}
              {agentSlugError ? (
                <p
                  id="agent-builder-commit-name-error"
                  role="alert"
                  className="text-destructive mt-2 text-sm"
                >
                  {agentSlugError}
                </p>
              ) : null}
            </div>
            <div className="bg-muted/25 rounded-xl p-3">
              <p className="text-muted-foreground text-xs">{copy.model}</p>
              <p className="mt-1 text-sm font-medium">{modelDisplayName}</p>
            </div>
            <div className="bg-muted/25 rounded-xl p-3">
              <p className="text-muted-foreground text-xs">
                {copy.capabilities}
              </p>
              <p className="mt-1 flex items-center gap-1.5 text-sm">
                <WrenchIcon aria-hidden className="size-3.5" />
                {copy.dependencySummary(
                  blueprint.tool_groups.length,
                  blueprint.skill_refs.length,
                  blueprint.mcp_version_ids.length,
                )}
              </p>
            </div>
          </section>

          {assumptions.length > 0 || conflicts.length > 0 ? (
            <section className="border-border/70 space-y-4 rounded-2xl border p-4">
              {assumptions.length > 0 ? (
                <div>
                  <h3 className="text-sm font-semibold">
                    {copy.assumptionsTitle}
                  </h3>
                  <ul className="text-muted-foreground mt-2 list-disc space-y-1 pl-5 text-sm leading-6">
                    {assumptions.map((assumption, index) => (
                      <li key={`${index}:${assumption}`}>{assumption}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {conflicts.length > 0 ? (
                <div>
                  <h3 className="text-sm font-semibold">
                    {copy.conflictsTitle}
                  </h3>
                  <div className="mt-2 space-y-2">
                    {conflicts.map((conflict, index) => (
                      <div
                        key={`${index}:${conflict.code}:${conflict.fields.join(",")}`}
                        role={
                          conflict.severity === "error" ? "alert" : undefined
                        }
                        className={
                          conflict.severity === "error"
                            ? "border-destructive/30 bg-destructive/5 rounded-xl border p-3 text-sm"
                            : "border-border/70 bg-muted/20 rounded-xl border p-3 text-sm"
                        }
                      >
                        <p className="leading-6">{conflict.message}</p>
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          <span className="text-muted-foreground text-xs">
                            {copy.conflictDocuments}
                          </span>
                          {conflict.fields.map((field) => (
                            <button
                              key={field}
                              type="button"
                              aria-label={copy.openConflictDocument(
                                CONFLICT_FIELD_FILES[field],
                              )}
                              className="border-border bg-background hover:bg-accent rounded-md border px-2 py-1 font-mono text-xs transition-colors"
                              onClick={() => {
                                onSelectedFieldChange(field);
                                setSurface("documents");
                              }}
                            >
                              {CONFLICT_FIELD_FILES[field]}
                            </button>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                  {hasBlockingConflict ? (
                    <p className="text-destructive mt-2 text-xs">
                      {copy.blockingConflictHint}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </section>
          ) : null}

          {mcpDependencyLoading ? (
            <p role="status" className="text-muted-foreground text-sm">
              {copy.checkingMcp}
            </p>
          ) : mcpDependencyBlockReason ? (
            <p role="alert" className="text-destructive text-sm">
              {mcpDependencyBlockReason}
            </p>
          ) : null}

          {!modelsLoading && !modelsError && !modelAvailable ? (
            <div
              role="alert"
              className="border-destructive/30 bg-destructive/5 text-destructive rounded-xl border px-4 py-3 text-sm"
            >
              <p className="font-medium">{copy.modelUnavailable}</p>
              <p className="mt-1 leading-6">{copy.modelRecovery}</p>
            </div>
          ) : null}
        </div>
      </div>

      <div
        id="agent-blueprint-documents-panel"
        role="tabpanel"
        aria-labelledby="agent-blueprint-documents-tab"
        hidden={surface !== "documents"}
        className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5"
      >
        <AgentInstructionWorkspace
          draft={blueprint}
          selectedField={selectedField}
          displayMode={displayMode}
          editing={effectiveEditing}
          canEdit={canAuthor}
          pending={pending}
          dirty={dirty}
          errorMessage={errorMessage ?? blueprintError}
          saveDisabledReason={blueprintError}
          saveTarget="blueprint"
          onSelect={onSelectedFieldChange}
          onDisplayModeChange={onDisplayModeChange}
          onChange={(field, value) =>
            onBlueprintChange({ ...blueprint, [field]: value })
          }
          onEdit={onEdit}
          onSave={onSave}
          onDiscard={onDiscard}
        />
      </div>

      {canAuthor && !effectiveEditing ? (
        <div
          data-agent-builder-blueprint-footer
          className="border-border/70 bg-background/95 flex shrink-0 flex-col gap-3 border-t px-3 pt-3 pb-4 backdrop-blur sm:flex-row sm:items-center sm:justify-between"
        >
          <div className="min-w-0">
            <p className="text-muted-foreground text-xs leading-5">
              {copy.createHint}
            </p>
            {(errorMessage ?? blueprintError) ? (
              <p role="alert" className="text-destructive mt-1 text-xs">
                {errorMessage ?? blueprintError}
              </p>
            ) : null}
          </div>
          <Button
            type="button"
            className="min-h-11 w-full sm:w-auto"
            disabled={
              !canAuthor ||
              !canCreate ||
              pending ||
              dirty ||
              Boolean(blueprintError) ||
              Boolean(agentSlugError) ||
              !agentSlug ||
              hasBlockingConflict ||
              !modelAvailable ||
              mcpDependencyLoading ||
              Boolean(mcpDependencyBlockReason)
            }
            onClick={onCreate}
          >
            {creating ? (
              <Loader2Icon aria-hidden className="size-4 animate-spin" />
            ) : null}
            {creating ? copy.creating : copy.createAgent}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
