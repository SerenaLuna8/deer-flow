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
import {
  SharedAssetApiError,
  invalidateProjectAssetQueries,
  useUpdateProjectAgentInstructions,
  type AssetVersion,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { SafeStreamdown } from "@/core/streamdown/components";

type AgentAssetVersion = Extract<AssetVersion, { agent_id: string }>;

export const AGENT_INSTRUCTION_FILES = [
  {
    name: "AGENTS.md",
    field: "agents_instructions",
    description: "Agent 的工作方式、边界与执行规则",
  },
  {
    name: "SOUL.md",
    field: "soul",
    description: "Agent 的人格、语气与价值取向",
  },
  {
    name: "IDENTITY.md",
    field: "identity",
    description: "Agent 对自身角色与身份的定义",
  },
  {
    name: "USER.md",
    field: "user_context",
    description: "Agent 需要长期遵循的用户背景与偏好",
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
  assetVersion: number;
};

type AgentInstructionConflictRecovery = {
  assetId: string;
  assetVersion: number;
  versionId: string | null;
};

export function agentInstructionSaveIsPending(
  pending: PendingSavedAgentVersion | null,
  item: Pick<ProjectAssetItem, "id" | "version">,
  version: Pick<AgentAssetVersion, "id"> | null,
): boolean {
  if (pending?.assetId !== item.id) return false;
  if (item.version < pending.assetVersion) return true;
  return (
    item.version === pending.assetVersion && version?.id !== pending.versionId
  );
}

export function agentInstructionConflictHasLatestServerState(
  conflict: AgentInstructionConflictRecovery | null,
  item: Pick<
    ProjectAssetItem,
    "id" | "version" | "current_published_version_id"
  >,
  version: Pick<AgentAssetVersion, "id"> | null,
): boolean {
  if (conflict?.assetId !== item.id || item.version <= conflict.assetVersion) {
    return false;
  }
  return item.current_published_version_id
    ? version?.id === item.current_published_version_id
    : version?.id !== conflict.versionId;
}

function selectedInstructionFile(field: AgentInstructionField) {
  return (
    AGENT_INSTRUCTION_FILES.find((file) => file.field === field) ??
    AGENT_INSTRUCTION_FILES[0]
  );
}

export function AgentInstructionWorkspace({
  draft,
  selectedField,
  displayMode,
  editing,
  canEdit = true,
  pending,
  dirty,
  errorMessage,
  saveDisabledReason = null,
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
  pending: boolean;
  dirty: boolean;
  errorMessage: string | null;
  saveDisabledReason?: string | null;
  onSelect: (field: AgentInstructionField) => void;
  onDisplayModeChange: (mode: "source" | "preview") => void;
  onChange: (field: AgentInstructionField, value: string) => void;
  onEdit: () => void;
  onSave: () => void;
  onDiscard: () => void;
}) {
  const selectedFile = selectedInstructionFile(selectedField);
  const content = draft[selectedField];

  return (
    <section className="space-y-4" aria-label="Agent 指令文件">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h3 className="text-sm font-semibold">指令文件</h3>
          <p className="text-muted-foreground mt-1 max-w-2xl text-xs leading-5">
            四个固定文档映射到当前 Agent 设置。保存后将用于后续运行。
          </p>
        </div>
        {canEdit && !editing ? (
          <Button type="button" size="sm" variant="outline" onClick={onEdit}>
            <PencilIcon aria-hidden className="size-4" />
            编辑指令
          </Button>
        ) : null}
      </div>

      <div className="border-border/70 overflow-hidden rounded-2xl border md:grid md:grid-cols-[220px_minmax(0,1fr)]">
        <div className="bg-muted/20 border-border/70 border-b p-3 md:border-r md:border-b-0">
          <p className="text-muted-foreground mb-2 px-2 text-xs font-medium">
            固定文件
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
                {selectedFile.description}
              </p>
            </div>
            <div
              className="bg-muted flex shrink-0 rounded-lg p-1"
              role="group"
              aria-label="文件显示方式"
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
                源码
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
                预览
              </Button>
            </div>
          </div>

          {displayMode === "preview" ? (
            <div className="prose prose-neutral dark:prose-invert min-h-[420px] max-w-none overflow-auto p-5 text-sm">
              {content ? (
                <SafeStreamdown>{content}</SafeStreamdown>
              ) : (
                <p className="text-muted-foreground">这个文件目前为空。</p>
              )}
            </div>
          ) : editing ? (
            <Textarea
              aria-label={`编辑 ${selectedFile.name}`}
              value={content}
              spellCheck={false}
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
            保存会同时更新四项 Agent 设置。
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              className="min-h-11 flex-1 sm:flex-none"
              disabled={pending}
              onClick={onDiscard}
            >
              放弃修改
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
              {pending ? "保存中…" : "保存设置"}
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
  editing: boolean;
  onEditingChange: (editing: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
  onVersionCreated: (versionId: string) => void;
}) {
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
  const expectedAssetVersionRef = useRef(item.version);
  const appliedServerStateRef = useRef(
    `${item.id}:${item.version}:${version?.id ?? "empty"}`,
  );
  const pendingSavedVersionRef = useRef<PendingSavedAgentVersion | null>(null);
  const conflictRecoveryRef = useRef<AgentInstructionConflictRecovery | null>(
    null,
  );
  const queryClient = useQueryClient();
  const update = useUpdateProjectAgentInstructions(accountId, projectId);
  const dirty = agentInstructionDraftIsDirty(baseline, draft);
  const isEditing = canAuthor && editing;
  const serverState = `${item.id}:${item.version}:${version?.id ?? "empty"}`;

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (canAuthor || !editing) return;
    setLocalError(
      "Agent 状态或编辑权限已发生变化，本地修改仍保留在当前页面。恢复权限后可继续保存，离开前也可以先复制内容。",
    );
  }, [canAuthor, editing]);

  useEffect(() => {
    if (editing || !dirty) return;
    setDraft(baseline);
    setDiscardOpen(false);
    setLocalError(null);
    onDirtyChange(false);
  }, [baseline, dirty, editing, onDirtyChange]);

  useEffect(() => {
    const conflictRecovery = conflictRecoveryRef.current;
    if (conflictRecovery?.assetId === item.id) {
      if (
        agentInstructionConflictHasLatestServerState(
          conflictRecovery,
          {
            id: item.id,
            version: item.version,
            current_published_version_id: item.current_published_version_id,
          },
          version,
        )
      ) {
        const nextBaseline = agentInstructionDraft(version);
        setBaseline(nextBaseline);
        expectedAssetVersionRef.current = item.version;
        appliedServerStateRef.current = serverState;
        conflictRecoveryRef.current = null;
        setLocalError(
          agentInstructionDraftIsDirty(nextBaseline, draft)
            ? "已加载最新修订并保留本地修改。请检查四项内容；再次保存会以当前本地内容覆盖远端设置。"
            : "已加载最新修订，本地内容与远端一致，无需再次保存。",
        );
        return;
      }
    } else if (conflictRecovery) {
      conflictRecoveryRef.current = null;
    }

    const pendingSavedVersion = pendingSavedVersionRef.current;
    if (pendingSavedVersion?.assetId === item.id) {
      if (
        agentInstructionSaveIsPending(
          pendingSavedVersion,
          { id: item.id, version: item.version },
          version,
        )
      ) {
        return;
      }
      pendingSavedVersionRef.current = null;
    } else if (pendingSavedVersion) {
      pendingSavedVersionRef.current = null;
    }
    if (serverState === appliedServerStateRef.current || dirty) return;

    const nextDraft = agentInstructionDraft(version);
    setBaseline(nextDraft);
    setDraft(nextDraft);
    setLocalError(null);
    expectedAssetVersionRef.current = item.version;
    appliedServerStateRef.current = serverState;
  }, [
    dirty,
    draft,
    item.current_published_version_id,
    item.id,
    item.version,
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

  useEffect(
    () => () => {
      onDirtyChange(false);
    },
    [onDirtyChange],
  );

  function discardChanges() {
    setDraft(baseline);
    setDiscardOpen(false);
    setLocalError(null);
    onDirtyChange(false);
    onEditingChange(false);
  }

  async function saveInstructions() {
    setLocalError(null);
    try {
      const result = await update.mutateAsync({
        assetId: item.id,
        input: {
          ...draft,
          expected_asset_version: expectedAssetVersionRef.current,
        },
      });
      const nextVersion = result.data;
      if (!("agent_id" in nextVersion)) {
        setLocalError("服务返回了无效的 Agent 设置。请重试。");
        return;
      }
      const nextDraft = agentInstructionDraft(nextVersion);
      const savedAssetVersion = expectedAssetVersionRef.current + 1;
      pendingSavedVersionRef.current = {
        assetId: item.id,
        versionId: nextVersion.id,
        assetVersion: savedAssetVersion,
      };
      conflictRecoveryRef.current = null;
      expectedAssetVersionRef.current = savedAssetVersion;
      setBaseline(nextDraft);
      setDraft(nextDraft);
      onDirtyChange(false);
      onEditingChange(false);
      onVersionCreated(nextVersion.id);
    } catch (error) {
      if (
        error instanceof SharedAssetApiError &&
        error.code === "ASSET_CONFLICT"
      ) {
        conflictRecoveryRef.current = {
          assetId: item.id,
          assetVersion: expectedAssetVersionRef.current,
          versionId: version?.id ?? null,
        };
        setLocalError(
          "Agent 已在其他窗口发生变化。本地内容仍然保留，正在加载最新修订…",
        );
        void invalidateProjectAssetQueries(
          queryClient,
          accountId,
          projectId,
          "agents",
        ).catch(() => undefined);
      } else {
        setLocalError(adminAssetErrorMessage(error));
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
        pending={update.isPending}
        dirty={dirty}
        errorMessage={localError}
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

      <Dialog open={discardOpen} onOpenChange={setDiscardOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>放弃未保存修改？</DialogTitle>
            <DialogDescription>
              四个指令文件会恢复为当前保存的内容，此操作无法撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDiscardOpen(false)}
            >
              继续编辑
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={discardChanges}
            >
              放弃修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
