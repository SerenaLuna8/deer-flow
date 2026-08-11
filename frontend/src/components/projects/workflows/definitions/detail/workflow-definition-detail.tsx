"use client";

import { useMemo, useState } from "react";

import { flushWorkflowEditorBeforeAction } from "@/components/projects/workflows/workbench/workbench-flush-context";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import { serializeCanonicalJsonValue } from "@/core/project-workflows/canonical";
import {
  workflowCredentialGrantMutationRequestV1Schema,
  workflowDraftSaveRequestV1Schema,
  type WorkflowCredentialGrantMutationRequestV1,
  type WorkflowDefinitionResponseV1,
  type WorkflowDraftResponseV1,
  type WorkflowDraftSaveRequestV1,
  type WorkflowVersionResponseV1,
} from "@/core/project-workflows/definition-contracts";
import type { WorkflowEditorFlushRegistry } from "@/core/project-workflows/editor/flush-registry";
import type { WorkflowEditorStore } from "@/core/project-workflows/editor/store";
import { sha256Utf8 } from "@/core/project-workflows/sha256";
import type { WorkflowValidationIssueV1 } from "@/core/project-workflows/transport";
import {
  jsonSchemaSchema,
  jsonValueSchema,
  type JsonValue,
} from "@/core/project-workflows/types";
import type { Capability } from "@/core/projects/types";

export type WorkflowDefinitionPermissions = {
  canRead: boolean;
  canEdit: boolean;
  canPublish: boolean;
  canDraftGrant: boolean;
  canVersionGrant: boolean;
};

export function workflowDefinitionPermissions(
  capabilities: readonly Capability[],
): WorkflowDefinitionPermissions {
  const granted = new Set<Capability>(capabilities);
  const canEdit = granted.has("workflow.edit");
  const canPublish = granted.has("workflow.publish");
  const hasGrant = granted.has("workflow.credential.grant");
  return {
    canRead: granted.has("workflow.read"),
    canEdit,
    canPublish,
    canDraftGrant: canEdit && hasGrant,
    canVersionGrant: canPublish && hasGrant,
  };
}

function deepFreeze<T>(value: T, seen = new WeakSet<object>()): T {
  if (value === null || typeof value !== "object") return value;
  if (seen.has(value)) return value;
  seen.add(value);
  for (const nested of Object.values(value as Record<string, unknown>)) {
    deepFreeze(nested, seen);
  }
  return Object.freeze(value);
}

export function workflowDraftDocument(
  draft: WorkflowDraftResponseV1,
): WorkflowPersistedDocumentV1 {
  return structuredClone({ spec: draft.spec, canvas: draft.canvas });
}

export function workflowDraftSaveRequest(
  baseline: Pick<WorkflowDraftResponseV1, "revision">,
  current: WorkflowPersistedDocumentV1,
): WorkflowDraftSaveRequestV1 {
  return workflowDraftSaveRequestV1Schema.parse({
    expected_revision: baseline.revision,
    spec: structuredClone(current.spec),
    canvas: structuredClone(current.canvas),
  });
}

export function workflowDraftDocumentsEqual(
  left: WorkflowPersistedDocumentV1,
  right: WorkflowPersistedDocumentV1,
): boolean {
  return (
    serializeCanonicalJsonValue(left as unknown as JsonValue) ===
    serializeCanonicalJsonValue(right as unknown as JsonValue)
  );
}

export const WORKFLOW_SAVED_DRAFT_REQUIRED_MESSAGE =
  "检测到尚未保存的本地更改；未发送请求，请先保存草稿后重试。";

const WORKFLOW_EDITOR_CONTEXT_CHANGED_MESSAGE =
  "工作流编辑上下文已变化；未发送请求，请重新操作。";

export type WorkflowSavedDraftActionEditor = Readonly<{
  draft: WorkflowDraftResponseV1;
  flushRegistry: WorkflowEditorFlushRegistry;
  store: WorkflowEditorStore;
}>;

export type WorkflowSavedDraftActionResult<Result> =
  | Readonly<{ status: "flush_failed" }>
  | Readonly<{ status: "editor_changed"; message: string }>
  | Readonly<{ status: "unsaved_changes"; message: string }>
  | Readonly<{ status: "submitted"; result: Result }>;

/**
 * Flushes controlled editors, then re-reads the active editor authority before
 * allowing a server action that only accepts an already-saved Draft identity.
 * A flush that materializes a local change must never be paired with the old
 * server revision/checksum.
 */
export function runWorkflowSavedDraftAction<Result>(
  expectedEditor: WorkflowSavedDraftActionEditor,
  readActiveEditor: () => WorkflowSavedDraftActionEditor | null,
  submit: (authority: {
    draft: WorkflowDraftResponseV1;
    store: WorkflowEditorStore;
    submitted: WorkflowPersistedDocumentV1;
  }) => Result,
): WorkflowSavedDraftActionResult<Result> {
  let resolved:
    | Readonly<{ status: "editor_changed"; message: string }>
    | Readonly<{ status: "unsaved_changes"; message: string }>
    | Readonly<{
        status: "ready";
        draft: WorkflowDraftResponseV1;
        store: WorkflowEditorStore;
        submitted: WorkflowPersistedDocumentV1;
      }>;
  try {
    resolved = flushWorkflowEditorBeforeAction(
      expectedEditor.flushRegistry,
      () => {
        const activeEditor = readActiveEditor();
        if (activeEditor !== expectedEditor) {
          return {
            status: "editor_changed" as const,
            message: WORKFLOW_EDITOR_CONTEXT_CHANGED_MESSAGE,
          };
        }
        const submitted = activeEditor.store.getState().current;
        if (
          !workflowDraftDocumentsEqual(
            workflowDraftDocument(activeEditor.draft),
            submitted,
          )
        ) {
          return {
            status: "unsaved_changes" as const,
            message: WORKFLOW_SAVED_DRAFT_REQUIRED_MESSAGE,
          };
        }
        return {
          status: "ready" as const,
          draft: activeEditor.draft,
          store: activeEditor.store,
          submitted,
        };
      },
    );
  } catch {
    return { status: "flush_failed" };
  }

  if (resolved.status !== "ready") return resolved;
  return {
    status: "submitted",
    result: submit({
      draft: resolved.draft,
      store: resolved.store,
      submitted: resolved.submitted,
    }),
  };
}

/**
 * Applies a server validation result only while it still describes the exact
 * Draft snapshot submitted by the caller. A user edit (or a later save) makes
 * the result stale without mutating local validation/session state.
 */
export function applyWorkflowValidationIfCurrent(
  store: WorkflowEditorStore,
  submitted: WorkflowPersistedDocumentV1,
  issues: readonly WorkflowValidationIssueV1[],
): boolean {
  if (!workflowDraftDocumentsEqual(store.getState().current, submitted)) {
    return false;
  }
  store.setValidationIssues(issues);
  return true;
}

/**
 * Starts a clean publish epoch only when no local edit happened after the
 * publish request captured its immutable Draft snapshot.
 */
export function settleWorkflowPublishIfCurrent(
  store: WorkflowEditorStore,
  submitted: WorkflowPersistedDocumentV1,
): boolean {
  if (!workflowDraftDocumentsEqual(store.getState().current, submitted)) {
    return false;
  }
  store.beginPublishEpoch();
  return true;
}

export type WorkflowDefinitionConflict = Readonly<{
  baseline: Readonly<{ revision: number; draft_checksum: string }>;
  local: WorkflowPersistedDocumentV1;
  remote: WorkflowDraftResponseV1 | null;
}>;

export function createWorkflowDefinitionConflict(
  baseline: Pick<WorkflowDraftResponseV1, "revision" | "draft_checksum">,
  local: WorkflowPersistedDocumentV1,
  remote: WorkflowDraftResponseV1 | null = null,
): WorkflowDefinitionConflict {
  return deepFreeze({
    baseline: {
      revision: baseline.revision,
      draft_checksum: baseline.draft_checksum,
    },
    local: structuredClone(local),
    remote: remote === null ? null : structuredClone(remote),
  });
}

export type WorkflowDefinitionOperation =
  | "create"
  | "archive"
  | "save"
  | "publish"
  | "draft_grant"
  | "version_grant";

export type WorkflowDefinitionOperationKeys = {
  current(
    operation: WorkflowDefinitionOperation,
    requestIdentity: string,
  ): string;
  complete(
    operation: WorkflowDefinitionOperation,
    requestIdentity: string,
  ): void;
  clear(): void;
};

export function workflowDefinitionRequestIdentity(value: unknown): string {
  return sha256Utf8(serializeCanonicalJsonValue(jsonValueSchema.parse(value)));
}

export function createWorkflowDefinitionOperationKeys(
  generate: () => string,
): WorkflowDefinitionOperationKeys {
  const keys = new Map<
    WorkflowDefinitionOperation,
    { requestIdentity: string; key: string }
  >();
  return {
    current(operation, requestIdentity) {
      const existing = keys.get(operation);
      if (existing?.requestIdentity === requestIdentity) return existing.key;
      const next = generate();
      if (typeof next !== "string" || next.length === 0) {
        throw new Error("Workflow operation key generator returned no key");
      }
      keys.set(operation, { requestIdentity, key: next });
      return next;
    },
    complete(operation, requestIdentity) {
      if (keys.get(operation)?.requestIdentity === requestIdentity) {
        keys.delete(operation);
      }
    },
    clear() {
      keys.clear();
    },
  };
}

export function workflowCredentialSlotSchemaChecksum(
  payloadSchema: unknown,
): string {
  const schema = jsonSchemaSchema.parse(payloadSchema);
  return sha256Utf8(
    serializeCanonicalJsonValue(schema as unknown as JsonValue),
  );
}

export function WorkflowDefinitionConflictBanner({
  comparing,
  conflict,
  onCompare,
  onReload,
}: {
  comparing: boolean;
  conflict: WorkflowDefinitionConflict;
  onCompare: () => void;
  onReload: () => void;
}) {
  return (
    <Alert variant="destructive" data-testid="workflow-draft-conflict">
      <AlertTitle>服务器上的草稿已更新</AlertTitle>
      <AlertDescription className="space-y-3">
        <p>
          本地编辑仍然保留，系统不会自动覆盖。基线修订：
          {conflict.baseline.revision}。
        </p>
        {conflict.remote ? (
          <p data-testid="workflow-draft-conflict-comparison">
            服务器修订 {conflict.remote.revision}，本地仍基于修订
            {conflict.baseline.revision}。
          </p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button
            disabled={comparing}
            type="button"
            variant="outline"
            onClick={onCompare}
          >
            {comparing ? "正在读取服务器草稿" : "比较差异"}
          </Button>
          <Button type="button" variant="destructive" onClick={onReload}>
            重新加载服务器草稿
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}

type GrantTarget =
  | { kind: "draft"; slotId: string; checksum: string }
  | {
      kind: "version";
      slotId: string;
      checksum: string;
      versionId: string;
    };

function WorkflowCredentialGrantEditor({
  disabled,
  onDelete,
  onPut,
  target,
}: {
  disabled?: boolean;
  onDelete?: (target: GrantTarget) => void;
  onPut?: (
    target: GrantTarget,
    body: WorkflowCredentialGrantMutationRequestV1,
  ) => void;
  target: GrantTarget;
}) {
  const [credentialId, setCredentialId] = useState("");
  const [credentialVersionId, setCredentialVersionId] = useState("");
  const body = workflowCredentialGrantMutationRequestV1Schema.safeParse({
    credential_id: credentialId,
    expected_credential_version_id: credentialVersionId,
    expected_slot_schema_checksum: target.checksum,
  });

  return (
    <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto_auto]">
      <Input
        aria-label={`${target.slotId} Credential ID`}
        autoComplete="off"
        disabled={disabled}
        placeholder="Credential UUID"
        value={credentialId}
        onChange={(event) => setCredentialId(event.target.value)}
      />
      <Input
        aria-label={`${target.slotId} Credential version ID`}
        autoComplete="off"
        disabled={disabled}
        placeholder="Credential version UUID"
        value={credentialVersionId}
        onChange={(event) => setCredentialVersionId(event.target.value)}
      />
      <Button
        disabled={disabled === true || !body.success || onPut === undefined}
        type="button"
        onClick={() => {
          if (body.success) onPut?.(target, body.data);
        }}
      >
        绑定凭据
      </Button>
      <Button
        disabled={disabled === true || onDelete === undefined}
        type="button"
        variant="outline"
        onClick={() => onDelete?.(target)}
      >
        撤销绑定
      </Button>
    </div>
  );
}

export type WorkflowDefinitionGrantPanelProps = {
  canDraftGrant: boolean;
  canVersionGrant: boolean;
  definition: WorkflowDefinitionResponseV1;
  draft: WorkflowDraftResponseV1;
  versions: readonly WorkflowVersionResponseV1[];
  busy?: boolean;
  onPut?: (
    target: GrantTarget,
    body: WorkflowCredentialGrantMutationRequestV1,
  ) => void;
  onDelete?: (target: GrantTarget) => void;
};

export function WorkflowDefinitionGrantPanel({
  busy = false,
  canDraftGrant,
  canVersionGrant,
  definition,
  draft,
  onDelete,
  onPut,
  versions,
}: WorkflowDefinitionGrantPanelProps) {
  const draftTargets = useMemo(
    () =>
      (draft.spec.credential_slots ?? []).flatMap((slot) => {
        if (
          typeof slot.id !== "string" ||
          slot.payload_schema === null ||
          slot.payload_schema === undefined
        ) {
          return [];
        }
        try {
          return [
            {
              kind: "draft" as const,
              slotId: slot.id,
              checksum: workflowCredentialSlotSchemaChecksum(
                slot.payload_schema,
              ),
            },
          ];
        } catch {
          return [];
        }
      }),
    [draft],
  );

  return (
    <section aria-label="凭据绑定" className="space-y-4 p-4">
      <div>
        <h2 className="text-sm font-semibold">凭据绑定</h2>
        <p className="text-muted-foreground text-xs">
          Draft 意图和已发布版本授权分别保存，不进入画布历史。
        </p>
      </div>

      {versions.map((version) => (
        <section className="space-y-2" key={version.id}>
          <h3 className="text-xs font-medium">版本 {version.version_number}</h3>
          <p className="text-muted-foreground text-xs">
            {version.missing_required_credential_slot_ids.length > 0
              ? `缺少 ${version.missing_required_credential_slot_ids.length} 个凭据绑定`
              : "所需凭据已绑定"}
          </p>
          {canVersionGrant
            ? version.credential_slots.map((slot) => (
                <WorkflowCredentialGrantEditor
                  disabled={busy}
                  key={`${version.id}:${slot.slot_id}`}
                  target={{
                    kind: "version",
                    slotId: slot.slot_id,
                    checksum: slot.payload_schema_checksum,
                    versionId: version.id,
                  }}
                  onDelete={onDelete}
                  onPut={onPut}
                />
              ))
            : null}
        </section>
      ))}

      {canDraftGrant && definition.lifecycle === "active" ? (
        <section className="space-y-2">
          <h3 className="text-xs font-medium">Draft 授权意图</h3>
          {draftTargets.length === 0 ? (
            <p className="text-muted-foreground text-xs">
              当前 Draft 没有完整的凭据槽声明。
            </p>
          ) : (
            draftTargets.map((target) => (
              <WorkflowCredentialGrantEditor
                disabled={busy}
                key={`draft:${target.slotId}`}
                target={target}
                onDelete={onDelete}
                onPut={onPut}
              />
            ))
          )}
        </section>
      ) : null}
    </section>
  );
}

export type { GrantTarget as WorkflowDefinitionGrantTarget };
