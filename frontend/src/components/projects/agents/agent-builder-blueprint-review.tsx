import { BotIcon, Loader2Icon, WrenchIcon } from "lucide-react";

import {
  AgentInstructionWorkspace,
  type AgentInstructionField,
} from "@/components/projects/assets/agent-instructions-workbench";
import { Button } from "@/components/ui/button";
import {
  agentBuilderBlueprintValidationError,
  type AgentBuilderBlueprint,
} from "@/core/agent-builder";

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
  const blueprintError = agentBuilderBlueprintValidationError(blueprint);
  const effectiveEditing = editing && canAuthor;

  return (
    <section className="space-y-6" aria-labelledby="agent-blueprint-title">
      <div>
        <p className="text-muted-foreground text-xs font-medium">生成结果</p>
        <h2
          id="agent-blueprint-title"
          className="mt-1 text-xl font-semibold tracking-tight"
        >
          Agent 设计稿
        </h2>
        <p className="text-muted-foreground mt-2 text-sm leading-6">
          请检查模型生成的四项设置。你可以先编辑，确认后再创建 Agent。
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
            运行配置
          </h3>
          <p className="text-muted-foreground mt-2 text-sm leading-6">
            {blueprint.description || "暂未生成 Agent 简介。"}
          </p>
        </div>
        <div className="bg-muted/25 rounded-xl p-3">
          <p className="text-muted-foreground text-xs">模型</p>
          <p className="mt-1 font-mono text-sm">{blueprint.model_ref}</p>
        </div>
        <div className="bg-muted/25 rounded-xl p-3">
          <p className="text-muted-foreground text-xs">能力与依赖</p>
          <p className="mt-1 flex items-center gap-1.5 text-sm">
            <WrenchIcon aria-hidden className="size-3.5" />
            {blueprint.tool_groups.length} 个工具组 ·{" "}
            {blueprint.skill_version_ids.length} 个 Skill ·{" "}
            {blueprint.mcp_version_ids.length} 个 MCP
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
          正在检查 MCP 依赖…
        </p>
      ) : mcpDependencyBlockReason ? (
        <p role="alert" className="text-destructive text-sm">
          {mcpDependencyBlockReason}
        </p>
      ) : null}

      {canAuthor && !effectiveEditing ? (
        <div className="border-border/70 bg-background/95 flex flex-col gap-3 rounded-2xl border p-3 shadow-lg backdrop-blur sm:flex-row sm:items-center sm:justify-between">
          <p className="text-muted-foreground text-xs leading-5">
            创建后默认停用，需手动启用
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
            {creating ? "正在创建…" : "创建 Agent"}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
