"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  Code2Icon,
  EyeIcon,
  FileTextIcon,
  Loader2Icon,
  PencilIcon,
  SaveIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import {
  SharedAssetApiError,
  useUpdateProjectAgentInstructions,
  type AssetVersion,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { SafeStreamdown } from "@/core/streamdown/components";

import {
  cacheProjectAgentAuthoringReload,
  projectAgentAuthoringCacheEpochs,
  reloadProjectAgentAuthoringState,
} from "./agent-authoring-recovery";

type AgentAssetVersion = Extract<AssetVersion, { agent_id: string }>;

export const AGENT_INSTRUCTION_FILES = [
  {
    name: "AGENTS.md",
    field: "agents_instructions",
  },
  {
    name: "SOUL.md",
    field: "soul",
  },
  {
    name: "IDENTITY.md",
    field: "identity",
  },
  {
    name: "USER.md",
    field: "user_context",
  },
] as const;

export type AgentInstructionField =
  (typeof AGENT_INSTRUCTION_FILES)[number]["field"];

export type AgentInstructionDraft = Record<AgentInstructionField, string>;

export function agentInstructionDraft(
  version: Pick<
    AgentAssetVersion,
    "agents_instructions" | "soul" | "identity" | "user_context"
  > | null,
): AgentInstructionDraft {
  return {
    agents_instructions: version?.agents_instructions ?? "",
    soul: version?.soul ?? "",
    identity: version?.identity ?? "",
    user_context: version?.user_context ?? "",
  };
}

export function agentInstructionDraftIsDirty(
  baseline: AgentInstructionDraft,
  draft: AgentInstructionDraft,
): boolean {
  return AGENT_INSTRUCTION_FILES.some(
    ({ field }) => baseline[field] !== draft[field],
  );
}

type PendingSavedAgentVersion = {
  assetId: string;
  versionId: string;
  assetRevision: number;
};

type AgentInstructionConflictRecovery = {
  assetId: string;
  assetRevision: number;
  generation: number;
  status: "refreshing" | "error";
};

export function agentInstructionSaveIsPending(
  pending: PendingSavedAgentVersion | null,
  item: Pick<ProjectAssetItem, "id" | "revision">,
  version: Pick<AgentAssetVersion, "id"> | null,
): boolean {
  if (pending?.assetId !== item.id) return false;
  if (item.revision < pending.assetRevision) return true;
  return (
    item.revision === pending.assetRevision && version?.id !== pending.versionId
  );
}

function selectedInstructionFile(field: AgentInstructionField) {
  return (
    AGENT_INSTRUCTION_FILES.find((file) => file.field === field) ??
    AGENT_INSTRUCTION_FILES[0]
  );
}

function instructionFileDescription(
  field: AgentInstructionField,
  copy: Translations["agents"]["instructions"]["files"],
): string {
  switch (field) {
    case "agents_instructions":
      return copy.agents;
    case "soul":
      return copy.soul;
    case "identity":
      return copy.identity;
    case "user_context":
      return copy.user;
  }
}

export function AgentInstructionWorkspace({
  draft,
  selectedField,
  displayMode,
  editing,
  canEdit = true,
  readOnly = false,
  historical = false,
  pending,
  inputDisabled = false,
  dirty,
  errorMessage,
  saveDisabledReason = null,
  saveTarget = "draft",
  onSelect,
  onDisplayModeChange,
  onChange,
  onEdit,
  onSave,
  onDiscard,
}: {
  draft: AgentInstructionDraft;
  selectedField: AgentInstructionField;
  displayMode: "source" | "preview";
  editing: boolean;
  canEdit?: boolean;
  readOnly?: boolean;
  historical?: boolean;
  pending: boolean;
  inputDisabled?: boolean;
  dirty: boolean;
  errorMessage: string | null;
  saveDisabledReason?: string | null;
  saveTarget?: "blueprint" | "draft";
  onSelect: (field: AgentInstructionField) => void;
  onDisplayModeChange: (mode: "source" | "preview") => void;
  onChange: (field: AgentInstructionField, value: string) => void;
  onEdit: () => void;
  onSave: () => void;
  onDiscard: () => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.instructions;
  const selectedFile = selectedInstructionFile(selectedField);
  const content = draft[selectedField];

  return (
    <section className="space-y-4" aria-label={copy.sectionAria}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h3 className="text-sm font-semibold">{copy.title}</h3>
          <p className="text-muted-foreground mt-1 max-w-2xl text-xs leading-5">
            {historical
              ? copy.historicalDescription
              : readOnly
                ? copy.readOnlyDescription
                : saveTarget === "draft"
                  ? copy.editDescription
                  : copy.blueprintDescription}
          </p>
        </div>
        {canEdit && !editing ? (
          <Button type="button" size="sm" variant="outline" onClick={onEdit}>
            <PencilIcon aria-hidden className="size-4" />
            {copy.edit}
          </Button>
        ) : null}
      </div>

      <div className="border-border/70 overflow-hidden rounded-2xl border md:grid md:grid-cols-[220px_minmax(0,1fr)]">
        <div className="bg-muted/20 border-border/70 border-b p-3 md:border-r md:border-b-0">
          <p className="text-muted-foreground mb-2 px-2 text-xs font-medium">
            {copy.fixedFiles}
          </p>
          <div className="space-y-1">
            {AGENT_INSTRUCTION_FILES.map((file) => {
              const selected = file.field === selectedField;
              return (
                <button
                  key={file.field}
                  type="button"
                  aria-current={selected ? "page" : undefined}
                  className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${
                    selected
                      ? "bg-background text-foreground shadow-sm"
                      : "text-muted-foreground hover:bg-background/70 hover:text-foreground"
                  }`}
                  onClick={() => onSelect(file.field)}
                >
                  <FileTextIcon aria-hidden className="size-4 shrink-0" />
                  <span className="truncate font-mono">{file.name}</span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="min-w-0">
          <div className="border-border/70 flex flex-col gap-3 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="truncate font-mono text-sm font-semibold">
                {selectedFile.name}
              </p>
              <p className="text-muted-foreground mt-1 text-xs">
                {instructionFileDescription(selectedFile.field, copy.files)}
              </p>
            </div>
            <div
              className="bg-muted flex shrink-0 rounded-lg p-1"
              role="group"
              aria-label={copy.displayMode}
            >
              <Button
                type="button"
                size="sm"
                variant={displayMode === "source" ? "secondary" : "ghost"}
                className="h-7 px-2"
                aria-pressed={displayMode === "source"}
                onClick={() => onDisplayModeChange("source")}
              >
                <Code2Icon aria-hidden className="size-3.5" />
                {copy.source}
              </Button>
              <Button
                type="button"
                size="sm"
                variant={displayMode === "preview" ? "secondary" : "ghost"}
                className="h-7 px-2"
                aria-pressed={displayMode === "preview"}
                onClick={() => onDisplayModeChange("preview")}
              >
                <EyeIcon aria-hidden className="size-3.5" />
                {copy.preview}
              </Button>
            </div>
          </div>

          {displayMode === "preview" ? (
            <div className="prose prose-neutral dark:prose-invert min-h-[420px] max-w-none overflow-auto p-5 text-sm">
              {content ? (
                <SafeStreamdown>{content}</SafeStreamdown>
              ) : (
                <p className="text-muted-foreground">{copy.empty}</p>
              )}
            </div>
          ) : editing ? (
            <Textarea
              aria-label={copy.editFile(selectedFile.name)}
              value={content}
              spellCheck={false}
              disabled={inputDisabled || pending}
              className="min-h-[420px] resize-y rounded-none border-0 p-5 font-mono text-sm leading-6 shadow-none focus-visible:ring-0"
              onChange={(event) => onChange(selectedField, event.target.value)}
            />
          ) : (
            <pre className="bg-muted/15 min-h-[420px] overflow-auto p-5 font-mono text-sm leading-6 whitespace-pre-wrap">
              <code>{content}</code>
            </pre>
          )}
        </div>
      </div>

      {errorMessage ? (
        <p role="alert" className="text-destructive text-sm">
          {errorMessage}
        </p>
      ) : null}

      {editing ? (
        <div className="bg-background/95 sticky bottom-0 z-10 flex flex-col gap-3 rounded-xl border p-3 shadow-lg backdrop-blur sm:flex-row sm:items-center sm:justify-between">
          <p className="text-muted-foreground text-xs">
            {saveTarget === "draft"
              ? copy.candidateSaveHint
              : copy.blueprintSaveHint}
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              className="min-h-11 flex-1 sm:flex-none"
              disabled={pending}
              onClick={onDiscard}
            >
              {copy.discard}
            </Button>
            <Button
              type="button"
              className="min-h-11 flex-1 sm:flex-none"
              disabled={!dirty || pending || Boolean(saveDisabledReason)}
              title={saveDisabledReason ?? undefined}
              onClick={onSave}
            >
              {pending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : (
                <SaveIcon aria-hidden className="size-4" />
              )}
              {pending ? copy.saving : copy.save}
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

export function AgentInstructionsWorkbench({
  accountId,
  projectId,
  item,
  version,
  canAuthor,
  authoringPreparationPending = false,
  editing,
  onEditingChange,
  onDirtyChange,
  onVersionCreated,
}: {
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: AgentAssetVersion | null;
  canAuthor: boolean;
  authoringPreparationPending?: boolean;
  editing: boolean;
  onEditingChange: (editing: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
  onVersionCreated: (versionId: string) => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.instructions;
  const initialDraft = agentInstructionDraft(version);
  const [baseline, setBaseline] = useState<AgentInstructionDraft>(initialDraft);
  const [draft, setDraft] = useState<AgentInstructionDraft>(initialDraft);
  const [selectedField, setSelectedField] = useState<AgentInstructionField>(
    "agents_instructions",
  );
  const [displayMode, setDisplayMode] = useState<"source" | "preview">(
    "source",
  );
  const [discardOpen, setDiscardOpen] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const expectedRevisionRef = useRef(item.revision);
  const appliedServerStateRef = useRef(
    `${item.id}:${item.revision}:${version?.id ?? "empty"}`,
  );
  const pendingSavedVersionRef = useRef<PendingSavedAgentVersion | null>(null);
  const [conflictRecovery, setConflictRecovery] =
    useState<AgentInstructionConflictRecovery | null>(null);
  const recoveryGenerationRef = useRef(0);
  const recoveryAbortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(false);
  const scopeKey = `${accountId}:${projectId}:${item.id}`;
  const scopeKeyRef = useRef(scopeKey);
  const authoringBaseVersionIdRef = useRef<string | null>(null);
  const queryClient = useQueryClient();
  const privateWork = usePrivateWorkAccess();
  const update = useUpdateProjectAgentInstructions(accountId, projectId);
  const dirty = agentInstructionDraftIsDirty(baseline, draft);
  const isEditing = canAuthor && editing;
  const serverState = `${item.id}:${item.revision}:${version?.id ?? "empty"}`;

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (canAuthor || !editing) return;
    setLocalError(copy.permissionLost);
  }, [canAuthor, copy.permissionLost, editing]);

  useEffect(() => {
    if (editing || !dirty) return;
    setDraft(baseline);
    setDiscardOpen(false);
    setLocalError(null);
    onDirtyChange(false);
  }, [baseline, dirty, editing, onDirtyChange]);

  useEffect(() => {
    if (conflictRecovery) return;

    const pendingSavedVersion = pendingSavedVersionRef.current;
    if (pendingSavedVersion?.assetId === item.id) {
      if (
        agentInstructionSaveIsPending(
          pendingSavedVersion,
          { id: item.id, revision: item.revision },
          version,
        )
      ) {
        return;
      }
      pendingSavedVersionRef.current = null;
    } else if (pendingSavedVersion) {
      pendingSavedVersionRef.current = null;
    }
    if (
      authoringBaseVersionIdRef.current &&
      editing &&
      version?.id !== authoringBaseVersionIdRef.current
    ) {
      return;
    }
    authoringBaseVersionIdRef.current = null;
    if (serverState === appliedServerStateRef.current || dirty) return;

    const nextDraft = agentInstructionDraft(version);
    setBaseline(nextDraft);
    setDraft(nextDraft);
    setLocalError(null);
    expectedRevisionRef.current = item.revision;
    appliedServerStateRef.current = serverState;
  }, [
    conflictRecovery,
    dirty,
    draft,
    editing,
    item.id,
    item.revision,
    serverState,
    version,
  ]);

  useEffect(() => {
    if (!dirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [dirty]);

  useEffect(() => {
    mountedRef.current = true;
    scopeKeyRef.current = scopeKey;
    return () => {
      mountedRef.current = false;
      recoveryAbortRef.current?.abort();
      recoveryAbortRef.current = null;
      recoveryGenerationRef.current += 1;
      onDirtyChange(false);
    };
  }, [onDirtyChange, scopeKey]);

  function recoveryIsCurrent(
    recovery: AgentInstructionConflictRecovery,
    controller: AbortController,
  ): boolean {
    return (
      mountedRef.current &&
      scopeKeyRef.current === scopeKey &&
      !controller.signal.aborted &&
      recoveryAbortRef.current === controller &&
      isPrivateWorkAccessActive(privateWork) &&
      recoveryGenerationRef.current === recovery.generation &&
      recovery.assetId === item.id
    );
  }

  function cancelRecovery() {
    recoveryAbortRef.current?.abort();
    recoveryAbortRef.current = null;
    recoveryGenerationRef.current += 1;
  }

  function discardChanges() {
    cancelRecovery();
    setConflictRecovery(null);
    authoringBaseVersionIdRef.current = null;
    setDraft(baseline);
    setDiscardOpen(false);
    setLocalError(null);
    onDirtyChange(false);
    onEditingChange(false);
  }

  async function recoverFromConflict(
    recovery: AgentInstructionConflictRecovery,
    controller: AbortController,
  ) {
    const startedAt = projectAgentAuthoringCacheEpochs({
      queryClient,
      accountId,
      projectId,
      assetId: recovery.assetId,
    });
    try {
      const reload = await runPrivateWorkAbortable(privateWork, (scopeSignal) =>
        reloadProjectAgentAuthoringState({
          projectId,
          assetId: recovery.assetId,
          attemptedRevision: recovery.assetRevision,
          signal: scopeSignal
            ? AbortSignal.any([controller.signal, scopeSignal])
            : controller.signal,
        }),
      );
      if (!recoveryIsCurrent(recovery, controller)) return;
      await cacheProjectAgentAuthoringReload({
        queryClient,
        accountId,
        projectId,
        assetId: recovery.assetId,
        reload,
        startedAt,
        isCurrent: () => recoveryIsCurrent(recovery, controller),
      });
      if (!recoveryIsCurrent(recovery, controller)) return;
      const nextBaseline = agentInstructionDraft(reload.version);
      setBaseline(nextBaseline);
      expectedRevisionRef.current = reload.item.revision;
      appliedServerStateRef.current = `${reload.item.id}:${reload.item.revision}:${reload.version.id}`;
      pendingSavedVersionRef.current = null;
      authoringBaseVersionIdRef.current = reload.version.id;
      setConflictRecovery(null);
      setLocalError(
        agentInstructionDraftIsDirty(nextBaseline, draft)
          ? copy.recoveryPreserved
          : copy.recoverySynced,
      );
    } catch {
      if (!recoveryIsCurrent(recovery, controller)) return;
      setConflictRecovery({ ...recovery, status: "error" });
      setLocalError(copy.recoveryFailed);
    } finally {
      if (recoveryAbortRef.current === controller) {
        recoveryAbortRef.current = null;
      }
    }
  }

  function retryConflictRecovery() {
    if (!conflictRecovery) return;
    recoveryAbortRef.current?.abort();
    const controller = new AbortController();
    const recovery: AgentInstructionConflictRecovery = {
      ...conflictRecovery,
      generation: recoveryGenerationRef.current + 1,
      status: "refreshing",
    };
    recoveryAbortRef.current = controller;
    recoveryGenerationRef.current = recovery.generation;
    setConflictRecovery(recovery);
    setLocalError(copy.recoveryReloading);
    void recoverFromConflict(recovery, controller);
  }

  async function saveInstructions() {
    const saveScopeKey = scopeKey;
    const saveGeneration = recoveryGenerationRef.current;
    setLocalError(null);
    try {
      const result = await update.mutateAsync({
        assetId: item.id,
        input: {
          ...draft,
          expected_revision: expectedRevisionRef.current,
        },
      });
      if (
        !mountedRef.current ||
        scopeKeyRef.current !== saveScopeKey ||
        !isPrivateWorkAccessActive(privateWork) ||
        recoveryGenerationRef.current !== saveGeneration
      ) {
        return;
      }
      const nextVersion = result.data;
      if (!("agent_id" in nextVersion)) {
        setLocalError(copy.invalidResponse);
        return;
      }
      const nextDraft = agentInstructionDraft(nextVersion);
      const savedRevision = expectedRevisionRef.current + 1;
      pendingSavedVersionRef.current = {
        assetId: item.id,
        versionId: nextVersion.id,
        assetRevision: savedRevision,
      };
      setConflictRecovery(null);
      authoringBaseVersionIdRef.current = null;
      expectedRevisionRef.current = savedRevision;
      setBaseline(nextDraft);
      setDraft(nextDraft);
      onDirtyChange(false);
      onEditingChange(false);
      onVersionCreated(nextVersion.id);
    } catch (error) {
      if (
        !mountedRef.current ||
        scopeKeyRef.current !== saveScopeKey ||
        !isPrivateWorkAccessActive(privateWork) ||
        recoveryGenerationRef.current !== saveGeneration
      ) {
        return;
      }
      if (
        error instanceof SharedAssetApiError &&
        error.code === "ASSET_CONFLICT"
      ) {
        recoveryAbortRef.current?.abort();
        const controller = new AbortController();
        const recovery: AgentInstructionConflictRecovery = {
          assetId: item.id,
          assetRevision: expectedRevisionRef.current,
          generation: recoveryGenerationRef.current + 1,
          status: "refreshing",
        };
        recoveryAbortRef.current = controller;
        recoveryGenerationRef.current = recovery.generation;
        setConflictRecovery(recovery);
        setLocalError(`${copy.conflictDetected} ${copy.recoveryReloading}`);
        void recoverFromConflict(recovery, controller);
      } else {
        setLocalError(adminAssetErrorMessage(error, t.adminAssets.errors));
      }
    }
  }

  return (
    <>
      <AgentInstructionWorkspace
        draft={draft}
        selectedField={selectedField}
        displayMode={displayMode}
        editing={isEditing}
        canEdit={canAuthor}
        readOnly={item.scope !== "project"}
        historical={version?.relation === "historical"}
        inputDisabled={
          update.isPending ||
          authoringPreparationPending ||
          conflictRecovery !== null ||
          !canAuthor
        }
        pending={update.isPending || authoringPreparationPending}
        dirty={dirty}
        errorMessage={localError}
        saveDisabledReason={
          conflictRecovery
            ? conflictRecovery.status === "error"
              ? copy.reloadRequired
              : copy.recoveryReloading
            : null
        }
        onSelect={(field) => {
          setSelectedField(field);
          setDisplayMode("source");
          setLocalError(null);
        }}
        onDisplayModeChange={setDisplayMode}
        onChange={(field, value) => {
          setDraft((current) => ({ ...current, [field]: value }));
          setLocalError(null);
        }}
        onEdit={() => {
          setLocalError(null);
          onEditingChange(true);
        }}
        onSave={() => void saveInstructions()}
        onDiscard={() => (dirty ? setDiscardOpen(true) : discardChanges())}
      />

      {conflictRecovery?.status === "error" ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={retryConflictRecovery}
        >
          {copy.reload}
        </Button>
      ) : null}

      <Dialog open={discardOpen} onOpenChange={setDiscardOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{copy.discardTitle}</DialogTitle>
            <DialogDescription>{copy.discardDescription}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDiscardOpen(false)}
            >
              {copy.continueEditing}
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={discardChanges}
            >
              {copy.discard}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
