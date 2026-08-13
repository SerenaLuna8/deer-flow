import { BotIcon, Loader2Icon, WrenchIcon } from "lucide-react";

import {
  AgentInstructionWorkspace,
  type AgentInstructionField,
} from "@/components/projects/assets/agent-instructions-workbench";
import { Button } from "@/components/ui/button";
import {
  agentBuilderBlueprintValidationIssue,
  type AgentBuilderBlueprint,
} from "@/core/agent-builder";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";

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

export function AgentBuilderBlueprintReview({
  blueprint,
  canAuthor,
  editing,
  pending,
  creating,
  dirty,
  canCreate,
  mcpDependencyLoading = false,
  mcpDependencyBlockReason = null,
  selectedField,
  displayMode,
  errorMessage,
  onSelectedFieldChange,
  onDisplayModeChange,
  onBlueprintChange,
  onEdit,
  onSave,
  onDiscard,
  onCreate,
}: {
  blueprint: AgentBuilderBlueprint;
  canAuthor: boolean;
  editing: boolean;
  pending: boolean;
  creating: boolean;
  dirty: boolean;
  canCreate: boolean;
  mcpDependencyLoading?: boolean;
  mcpDependencyBlockReason?: string | null;
  selectedField: AgentInstructionField;
  displayMode: "source" | "preview";
  errorMessage: string | null;
  onSelectedFieldChange: (field: AgentInstructionField) => void;
  onDisplayModeChange: (mode: "source" | "preview") => void;
  onBlueprintChange: (blueprint: AgentBuilderBlueprint) => void;
  onEdit: () => void;
  onSave: () => void;
  onDiscard: () => void;
  onCreate: () => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.builder.blueprint;
  const blueprintError = agentBuilderBlueprintValidationMessage(
    blueprint,
    copy.validation,
  );
  const effectiveEditing = editing && canAuthor;

  return (
    <section className="space-y-6" aria-labelledby="agent-blueprint-title">
      <div>
        <p className="text-muted-foreground text-xs font-medium">
          {copy.result}
        </p>
        <h2
          id="agent-blueprint-title"
          className="mt-1 text-xl font-semibold tracking-tight"
        >
          {copy.title}
        </h2>
        <p className="text-muted-foreground mt-2 text-sm leading-6">
          {copy.description}
        </p>
      </div>

      <section
        aria-labelledby="agent-runtime-title"
        className="border-border/70 grid gap-4 rounded-2xl border p-4 sm:grid-cols-2 sm:p-5"
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
        <div className="bg-muted/25 rounded-xl p-3">
          <p className="text-muted-foreground text-xs">{copy.model}</p>
          <p className="mt-1 font-mono text-sm">{blueprint.model_ref}</p>
        </div>
        <div className="bg-muted/25 rounded-xl p-3">
          <p className="text-muted-foreground text-xs">{copy.capabilities}</p>
          <p className="mt-1 flex items-center gap-1.5 text-sm">
            <WrenchIcon aria-hidden className="size-3.5" />
            {copy.dependencySummary(
              blueprint.tool_groups.length,
              blueprint.skill_version_ids.length,
              blueprint.mcp_version_ids.length,
            )}
          </p>
        </div>
      </section>

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

      {mcpDependencyLoading ? (
        <p role="status" className="text-muted-foreground text-sm">
          {copy.checkingMcp}
        </p>
      ) : mcpDependencyBlockReason ? (
        <p role="alert" className="text-destructive text-sm">
          {mcpDependencyBlockReason}
        </p>
      ) : null}

      {canAuthor && !effectiveEditing ? (
        <div className="border-border/70 bg-background/95 flex flex-col gap-3 rounded-2xl border p-3 shadow-lg backdrop-blur sm:flex-row sm:items-center sm:justify-between">
          <p className="text-muted-foreground text-xs leading-5">
            {copy.createHint}
          </p>
          <Button
            type="button"
            className="min-h-12 w-full sm:w-auto"
            disabled={
              !canAuthor ||
              !canCreate ||
              pending ||
              dirty ||
              Boolean(blueprintError) ||
              mcpDependencyLoading ||
              Boolean(mcpDependencyBlockReason)
            }
            onClick={onCreate}
          >
            {creating ? (
              <Loader2Icon aria-hidden className="size-4 animate-spin" />
            ) : null}
            {creating ? copy.creating : copy.createDraft}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
