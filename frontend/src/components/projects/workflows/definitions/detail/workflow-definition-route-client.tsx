"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { WorkflowCanvas } from "@/components/projects/workflows/canvas/workflow-canvas";
import {
  focusWorkflowInspectorIssue,
  WorkflowInspector,
} from "@/components/projects/workflows/inspector/workflow-inspector";
import {
  createWorkflowConnectCommand,
  createWorkflowNextStepCommand,
  createWorkflowPaletteNodeCommand,
} from "@/components/projects/workflows/node-config/workflow-node-commands";
import {
  flushWorkflowEditorBeforeAction,
  WorkflowWorkbenchFlushProvider,
} from "@/components/projects/workflows/workbench/workbench-flush-context";
import {
  updateWorkflowEditorSession,
  useWorkflowWorkbenchSnapshot,
  useWorkflowWorkbenchStore,
  WorkflowWorkbenchStoreProvider,
} from "@/components/projects/workflows/workbench/workbench-store-context";
import { WorkflowWorkbench } from "@/components/projects/workflows/workbench/workflow-workbench";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { useModels } from "@/core/models/hooks";
import {
  ProjectWorkflowApiError,
  readProjectWorkflowNodeCatalog,
  readProjectWorkflowReadiness,
} from "@/core/project-workflows/api";
import type {
  NodeCatalogEntry,
  NodeCatalogResponseV1,
} from "@/core/project-workflows/catalog";
import {
  createWorkflowDefinitionIdempotencyKey,
  createWorkflowDefinitionTransport,
} from "@/core/project-workflows/definition-api";
import type {
  WorkflowCredentialGrantMutationRequestV1,
  WorkflowDraftResponseV1,
  WorkflowDraftValidationResponseV1,
  WorkflowPublishResponseV1,
  WorkflowVersionResponseV1,
} from "@/core/project-workflows/definition-contracts";
import {
  useDeleteWorkflowDraftGrantIntent,
  usePublishWorkflowDraft,
  usePutWorkflowDraftGrantIntent,
  usePutWorkflowVersionGrant,
  useRevokeWorkflowVersionGrant,
  useSaveWorkflowDraft,
  useValidateWorkflowDraft,
  useWorkflowDefinition,
  useWorkflowDraft,
  useWorkflowVersion,
  useWorkflowVersions,
} from "@/core/project-workflows/definition-queries";
import {
  createWorkflowEditorFlushRegistry,
  type WorkflowEditorFlushRegistry,
} from "@/core/project-workflows/editor/flush-registry";
import {
  resolveDraftNodePorts,
  type WorkflowDraftPortLocale,
} from "@/core/project-workflows/editor/ports";
import {
  createWorkflowEditorStore,
  type WorkflowEditorStore,
} from "@/core/project-workflows/editor/store";
import {
  validateWorkflowControlConnectionV1,
  type WorkflowValidationIssueTarget,
} from "@/core/project-workflows/editor/validation";
import {
  useProjectWorkflowNodeCatalog,
  useProjectWorkflowReadiness,
} from "@/core/project-workflows/hooks";
import type { WorkflowValidationIssueV1 } from "@/core/project-workflows/transport";

import { ProjectAccessDenied } from "../../../project-access-denied";
import { useCurrentProject } from "../../../project-context";

import {
  applyWorkflowValidationIfCurrent,
  createWorkflowDefinitionConflict,
  createWorkflowDefinitionOperationKeys,
  runWorkflowSavedDraftAction,
  settleWorkflowPublishIfCurrent,
  workflowDefinitionPermissions,
  workflowDefinitionRequestIdentity,
  workflowDraftDocument,
  workflowDraftSaveRequest,
  workflowDraftDocumentsEqual,
  WorkflowDefinitionConflictBanner,
  WorkflowDefinitionGrantPanel,
  type WorkflowDefinitionConflict,
  type WorkflowDefinitionGrantTarget,
} from "./workflow-definition-detail";

const definitionTransport = createWorkflowDefinitionTransport();
const projectControlTransport = {
  readProjectWorkflowNodeCatalog,
  readProjectWorkflowReadiness,
};

type EditorAuthority = {
  workflowId: string;
  draft: WorkflowDraftResponseV1;
  flushRegistry: WorkflowEditorFlushRegistry;
  store: WorkflowEditorStore;
};

type ValidationAuthority = {
  result: WorkflowDraftValidationResponseV1;
  submitted: ReturnType<WorkflowEditorStore["getState"]>["current"];
};

function uniqueVersions(
  pages: readonly { items: readonly WorkflowVersionResponseV1[] }[],
): WorkflowVersionResponseV1[] {
  const seen = new Set<string>();
  return pages.flatMap((page) =>
    page.items.filter((version) => {
      if (seen.has(version.id)) return false;
      seen.add(version.id);
      return true;
    }),
  );
}

function mutationMessage(error: unknown): string {
  if (error instanceof ProjectWorkflowApiError) {
    if (error.code === "WORKFLOW_FORBIDDEN") {
      return "当前项目权限已变化，操作已被拒绝。";
    }
    if (error.code === "WORKFLOW_DRAFT_INVALID") {
      return "当前草稿未通过服务端校验。";
    }
    if (error.code === "WORKFLOW_UNAVAILABLE") {
      return "工作流服务暂时不可用，请稍后重试。";
    }
  }
  return "操作未完成；本地草稿仍然保留。";
}

function definitiveFailure(error: unknown): boolean {
  return (
    error instanceof ProjectWorkflowApiError &&
    error.status >= 400 &&
    error.status < 500
  );
}

function WorkflowDetailLoading() {
  return (
    <main
      aria-busy="true"
      aria-label="正在加载工作流"
      className="space-y-3 p-6"
    >
      <Skeleton className="h-14 w-full" />
      <Skeleton className="h-[70vh] min-h-[36rem] w-full" />
    </main>
  );
}

function WorkflowDetailUnavailable({
  disabled = false,
  onRetry,
}: {
  disabled?: boolean;
  onRetry?: () => void;
}) {
  return (
    <main className="mx-auto flex min-h-[60vh] max-w-xl items-center p-6 text-center">
      <section
        className="bg-card w-full rounded-xl border p-8"
        data-testid={
          disabled ? "workflow-detail-disabled" : "workflow-detail-unavailable"
        }
      >
        <h1 className="text-xl font-semibold">
          {disabled ? "工作流功能未启用" : "工作流暂时不可用"}
        </h1>
        <p className="text-muted-foreground mt-3 text-sm">
          {disabled
            ? "管理员尚未为此项目启用工作流控制面。"
            : "当前无法安全读取工作流定义，请稍后重试。"}
        </p>
        {!disabled && onRetry ? (
          <Button className="mt-5" type="button" onClick={onRetry}>
            重试
          </Button>
        ) : null}
      </section>
    </main>
  );
}

function WorkflowDefinitionPalette({
  disabled,
  entries,
  locale = "zh-CN",
}: {
  disabled: boolean;
  entries: readonly NodeCatalogEntry[];
  locale?: WorkflowDraftPortLocale;
}) {
  const store = useWorkflowWorkbenchStore();
  const snapshot = useWorkflowWorkbenchSnapshot();
  const hasStart = (snapshot.current.spec.nodes ?? []).some(
    (node) => node.type === "start",
  );

  const addNode = (nodeType: NodeCatalogEntry["definition"]["type"]) => {
    const index = (store.getState().current.spec.nodes ?? []).length;
    const command = createWorkflowPaletteNodeCommand({
      nodeId: crypto.randomUUID(),
      nodeType,
      position: {
        x: (index % 4) * 320,
        y: Math.floor(index / 4) * 220,
      },
    });
    if (command === null) return;
    const result = store.dispatch(command);
    if (!result.applied) return;
    updateWorkflowEditorSession(store, (session) => ({
      ...session,
      selection: { node_ids: [command.node.id as string], edge_ids: [] },
      inspector: {
        ...session.inspector,
        open: true,
        node_id: command.node.id as string,
      },
    }));
  };

  return (
    <div className="space-y-2 p-3">
      <h2 className="text-xs font-semibold tracking-wide uppercase">
        九类节点
      </h2>
      <ul className="space-y-1">
        {entries.map((entry) => (
          <li
            className="flex items-center justify-between rounded-md border px-3 py-2 text-xs"
            key={`${entry.definition.type}:${entry.definition.version}`}
          >
            <span>{entry.definition.title_i18n[locale]}</span>
            <div className="flex items-center gap-2">
              <Badge
                variant={
                  entry.availability.state === "enabled"
                    ? "secondary"
                    : "outline"
                }
              >
                {entry.availability.state === "enabled" ? "可用" : "不可用"}
              </Badge>
              <Button
                aria-label={`添加${entry.definition.title_i18n[locale]}节点`}
                disabled={
                  disabled ||
                  entry.availability.state !== "enabled" ||
                  (entry.definition.type === "start" && hasStart)
                }
                size="sm"
                type="button"
                variant="ghost"
                onClick={() => addNode(entry.definition.type)}
              >
                添加
              </Button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function WorkflowDefinitionCanvas({
  catalog,
  focusTarget,
  locale = "zh-CN",
  onCommandIssue,
  readOnly,
}: {
  catalog: NodeCatalogResponseV1;
  focusTarget: WorkflowValidationIssueTarget | null;
  locale?: WorkflowDraftPortLocale;
  onCommandIssue: (message: string) => void;
  readOnly: boolean;
}) {
  const store = useWorkflowWorkbenchStore();
  const snapshot = useWorkflowWorkbenchSnapshot();
  const reportResult = (
    result: ReturnType<typeof store.dispatch>,
    fallback: string,
  ) => {
    if (!result.applied) {
      onCommandIssue(result.issues[0]?.message ?? fallback);
    }
  };
  return (
    <WorkflowCanvas
      canvas={snapshot.current.canvas}
      catalog={catalog}
      focusTarget={focusTarget}
      locale={locale}
      readOnly={readOnly}
      spec={snapshot.current.spec}
      isValidConnection={(connection) => {
        const command = createWorkflowConnectCommand({
          connection,
          edgeId: crypto.randomUUID(),
        });
        return (
          command !== null &&
          !validateWorkflowControlConnectionV1(
            store.getState().current,
            command.transition,
          ).some((issue) => issue.severity === "error")
        );
      }}
      onConnect={(connection) => {
        const command = createWorkflowConnectCommand({
          connection,
          edgeId: crypto.randomUUID(),
        });
        if (command === null) {
          onCommandIssue("连接端点不完整，未修改工作流。");
          return;
        }
        reportResult(store.dispatch(command), "无法创建该连接。");
      }}
      onEdgesDelete={(edges) => {
        reportResult(
          store.dispatch({
            type: "disconnect",
            edge_ids: edges.map((edge) => edge.id),
          }),
          "无法删除所选连接。",
        );
      }}
      onNodeClick={(_event, node) => {
        updateWorkflowEditorSession(store, (session) => ({
          ...session,
          selection: { node_ids: [node.id], edge_ids: [] },
          inspector: {
            ...session.inspector,
            open: true,
            node_id: node.id,
          },
        }));
      }}
      onNodeDragStart={(_event, node, nodes) => {
        const selected = nodes.length > 0 ? nodes : [node];
        if (!store.beginNodeDrag?.(selected.map((item) => item.id))) {
          onCommandIssue("无法开始移动所选节点。");
        }
      }}
      onNodeDrag={(_event, node, nodes) => {
        const selected = nodes.length > 0 ? nodes : [node];
        for (const item of selected) {
          store.updateNodeDragPosition?.(item.id, item.position);
        }
      }}
      onNodeDragStop={(_event, node, nodes) => {
        const selected = nodes.length > 0 ? nodes : [node];
        for (const item of selected) {
          store.updateNodeDragPosition?.(item.id, item.position);
        }
        if (!store.commitNodeDrag?.()) {
          store.cancelNodeDrag?.();
          onCommandIssue("节点位置未能提交，已保留原布局。");
        }
      }}
      onNodesDelete={(nodes) => {
        const command = {
          type: "delete_nodes" as const,
          node_ids: nodes.map((node) => node.id),
        };
        const result = store.dispatch(command);
        if (result.requires_confirmation && result.deletion_impact) {
          const impact = result.deletion_impact;
          const confirmed = globalThis.confirm(
            `删除将同时移除 ${impact.transition_ids.length} 条连接，并解除 ${impact.binding_references.length} 个引用。是否继续？`,
          );
          if (confirmed) {
            reportResult(
              store.dispatch({ ...command, confirmed: true }),
              "无法删除所选节点。",
            );
          }
          return;
        }
        reportResult(result, "无法删除所选节点。");
      }}
    />
  );
}

function WorkflowDefinitionInspector({
  capabilities,
  catalog,
  disabled,
  locale,
  modelCatalog,
  onFocusIssue,
  readOnly,
}: {
  capabilities: Parameters<typeof WorkflowInspector>[0]["capabilities"];
  catalog: NodeCatalogResponseV1;
  disabled: boolean;
  locale: WorkflowDraftPortLocale;
  modelCatalog: NonNullable<
    Parameters<typeof WorkflowInspector>[0]["modelCatalog"]
  >;
  onFocusIssue: (target: WorkflowValidationIssueTarget) => void;
  readOnly: boolean;
}) {
  const snapshot = useWorkflowWorkbenchSnapshot();
  const selectedNodeId = snapshot.editorSession.inspector.node_id;
  const selected = (snapshot.current.spec.nodes ?? []).find(
    (node) => node.id === selectedNodeId,
  );
  const sourcePorts = selected?.id
    ? resolveDraftNodePorts(
        snapshot.current,
        selected.id,
        locale,
      ).outputPorts.filter((port) => port.kind === "control")
    : [];

  return (
    <WorkflowInspector
      capabilities={capabilities}
      catalog={catalog.entries}
      createNextStepCommand={(input) =>
        createWorkflowNextStepCommand({
          ...input,
          nextId: () => crypto.randomUUID(),
        })
      }
      disabled={disabled}
      locale={locale}
      modelCatalog={modelCatalog}
      readOnly={readOnly}
      sourcePorts={sourcePorts}
      onFocusIssue={onFocusIssue}
    />
  );
}

function WorkflowVersionReadOnlyPreview({
  catalog,
  locale,
  version,
}: {
  catalog: NodeCatalogResponseV1;
  locale: WorkflowDraftPortLocale;
  version: WorkflowVersionResponseV1;
}) {
  const [store] = useState(() =>
    createWorkflowEditorStore({
      document: {
        spec: structuredClone(version.spec),
        canvas: structuredClone(version.canvas),
      },
    }),
  );
  useEffect(() => () => store.dispose(), [store]);

  return (
    <WorkflowWorkbenchStoreProvider store={store}>
      <section
        aria-label={`版本 ${version.version_number} 只读画布`}
        className="bg-background h-80 overflow-hidden rounded-md border"
        data-testid="workflow-version-readonly-preview"
      >
        <WorkflowCanvas
          canvas={version.canvas}
          catalog={catalog}
          focusTarget={null}
          locale={locale}
          readOnly
          spec={version.spec}
        />
      </section>
    </WorkflowWorkbenchStoreProvider>
  );
}

export function WorkflowValidationAndVersionPanel({
  catalog,
  grantBusy,
  canDraftGrant,
  canVersionGrant,
  definition,
  draft,
  onDeleteGrant,
  onFocusIssue,
  onLoadMore,
  onPutGrant,
  publishResult,
  validation,
  grantVersions,
  versions,
  hasMoreVersions,
  loadingMoreVersions,
  locale = "zh-CN",
  currentVersionStatus,
  onRetryCurrentVersion,
  onRetryVersionHistory,
  onSelectVersion,
  selectedVersionId,
  versionHistoryStatus,
}: {
  catalog: NodeCatalogResponseV1;
  canDraftGrant: boolean;
  canVersionGrant: boolean;
  definition: NonNullable<ReturnType<typeof useWorkflowDefinition>["data"]>;
  draft: WorkflowDraftResponseV1;
  grantBusy: boolean;
  grantVersions: readonly WorkflowVersionResponseV1[];
  hasMoreVersions: boolean;
  loadingMoreVersions: boolean;
  locale?: WorkflowDraftPortLocale;
  currentVersionStatus: "none" | "loading" | "error" | "ready";
  onDeleteGrant: (target: WorkflowDefinitionGrantTarget) => void;
  onFocusIssue: (issue: WorkflowValidationIssueV1) => void;
  onLoadMore: () => void;
  onRetryCurrentVersion: () => void;
  onRetryVersionHistory: () => void;
  onSelectVersion: (versionId: string) => void;
  onPutGrant: (
    target: WorkflowDefinitionGrantTarget,
    body: WorkflowCredentialGrantMutationRequestV1,
  ) => void;
  publishResult: WorkflowPublishResponseV1 | null;
  selectedVersionId: string | null;
  validation: ValidationAuthority | null;
  versionHistoryStatus: "loading" | "error" | "ready";
  versions: readonly WorkflowVersionResponseV1[];
}) {
  const snapshot = useWorkflowWorkbenchSnapshot();
  const currentValidation =
    validation !== null &&
    workflowDraftDocumentsEqual(snapshot.current, validation.submitted)
      ? validation.result
      : null;

  return (
    <div className="grid min-h-full gap-4 p-4 lg:grid-cols-2">
      <section aria-label="校验结果" className="space-y-2">
        <h2 className="text-sm font-semibold">校验结果</h2>
        {currentValidation === null ? (
          <p className="text-muted-foreground text-xs">
            保存草稿后运行服务端校验。
          </p>
        ) : currentValidation.valid ? (
          <p className="text-xs text-emerald-700">服务端校验通过</p>
        ) : (
          <ul className="space-y-1">
            {currentValidation.issues.map((issue, index) => (
              <li key={`${issue.code}:${index}`}>
                <button
                  className="text-destructive text-left text-xs underline-offset-2 hover:underline"
                  type="button"
                  onClick={() => onFocusIssue(issue)}
                >
                  {issue.message}
                </button>
              </li>
            ))}
          </ul>
        )}
        {publishResult ? (
          <p className="text-xs" role="status">
            已发布版本 {publishResult.version_number}
            {publishResult.executable ? "，可执行" : "，等待凭据绑定"}
          </p>
        ) : null}

        <h2 className="pt-3 text-sm font-semibold">版本历史</h2>
        {versionHistoryStatus === "loading" ? (
          <p className="text-muted-foreground text-xs" role="status">
            正在加载版本历史…
          </p>
        ) : versionHistoryStatus === "error" ? (
          <div
            className="space-y-2"
            data-testid="workflow-version-history-error"
          >
            <p className="text-destructive text-xs" role="alert">
              版本历史暂时不可用，未将失败解释为空结果。
            </p>
            <Button
              size="sm"
              type="button"
              variant="outline"
              onClick={onRetryVersionHistory}
            >
              重试版本历史
            </Button>
          </div>
        ) : versions.length === 0 ? (
          <p className="text-muted-foreground text-xs">尚未发布版本</p>
        ) : (
          <ol className="space-y-1">
            {versions.map((version) => (
              <li
                className="flex items-center justify-between gap-2 text-xs"
                key={version.id}
              >
                <span>
                  版本 {version.version_number} ·{" "}
                  {version.executable ? "可执行" : "不可执行"}
                </span>
                <Button
                  aria-pressed={selectedVersionId === version.id}
                  disabled={grantBusy}
                  size="sm"
                  type="button"
                  variant={
                    selectedVersionId === version.id ? "secondary" : "ghost"
                  }
                  onClick={() => onSelectVersion(version.id)}
                >
                  {selectedVersionId === version.id
                    ? "正在查看（只读）"
                    : "只读查看"}
                </Button>
              </li>
            ))}
          </ol>
        )}
        {versionHistoryStatus === "ready" && hasMoreVersions ? (
          <Button
            disabled={loadingMoreVersions}
            size="sm"
            type="button"
            variant="outline"
            onClick={onLoadMore}
          >
            {loadingMoreVersions ? "加载中" : "加载更多版本"}
          </Button>
        ) : null}
      </section>

      <div className="space-y-2">
        {currentVersionStatus === "loading" ? (
          <p className="text-muted-foreground px-4 pt-4 text-xs" role="status">
            正在加载所选已发布版本的凭据状态…
          </p>
        ) : currentVersionStatus === "error" ? (
          <div
            className="space-y-2 px-4 pt-4"
            data-testid="workflow-current-version-error"
          >
            <p className="text-destructive text-xs" role="alert">
              所选已发布版本暂时不可用，未隐藏其凭据状态。
            </p>
            <Button
              size="sm"
              type="button"
              variant="outline"
              onClick={onRetryCurrentVersion}
            >
              重试所选版本
            </Button>
          </div>
        ) : null}
        <WorkflowDefinitionGrantPanel
          busy={grantBusy}
          canDraftGrant={canDraftGrant}
          canVersionGrant={canVersionGrant}
          definition={definition}
          draft={draft}
          versions={currentVersionStatus === "ready" ? grantVersions : []}
          onDelete={onDeleteGrant}
          onPut={onPutGrant}
        />
        {currentVersionStatus === "ready" && grantVersions[0] ? (
          <WorkflowVersionReadOnlyPreview
            key={grantVersions[0].id}
            catalog={catalog}
            locale={locale}
            version={grantVersions[0]}
          />
        ) : null}
      </div>
    </div>
  );
}

export function WorkflowDefinitionRouteClient({
  workflowId,
}: {
  workflowId: string;
}) {
  const project = useCurrentProject();
  const router = useRouter();
  const { locale } = useI18n();
  const permissions = workflowDefinitionPermissions(project.capabilities);
  const readiness = useProjectWorkflowReadiness(
    permissions.canRead,
    projectControlTransport,
  );
  const controlReady =
    readiness.data?.status === "ready" &&
    readiness.data.workflow_enabled &&
    readiness.data.schema_ready;
  const catalog = useProjectWorkflowNodeCatalog(
    permissions.canRead && controlReady,
    projectControlTransport,
  );
  const models = useModels({ enabled: permissions.canRead && controlReady });
  const modelCatalog: NonNullable<
    Parameters<typeof WorkflowInspector>[0]["modelCatalog"]
  > = {
    status: models.error
      ? "unavailable"
      : models.isLoading
        ? "loading"
        : "ready",
    models: models.models,
  };
  const definition = useWorkflowDefinition(
    workflowId,
    permissions.canRead && controlReady,
    definitionTransport,
  );
  const draft = useWorkflowDraft(
    workflowId,
    permissions.canRead && controlReady,
    definitionTransport,
  );
  const versions = useWorkflowVersions(
    workflowId,
    { limit: 50 },
    permissions.canRead && controlReady,
    definitionTransport,
  );
  const currentVersionId = definition.data?.current_published_version_id;
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(
    null,
  );
  const versionDetailId = selectedVersionId ?? currentVersionId ?? null;
  const versionDetailExpected = versionDetailId !== null;
  const versionDetail = useWorkflowVersion(
    workflowId,
    versionDetailId,
    permissions.canRead && controlReady && versionDetailExpected,
    definitionTransport,
  );

  const save = useSaveWorkflowDraft(workflowId, definitionTransport);
  const validate = useValidateWorkflowDraft(workflowId, definitionTransport);
  const publish = usePublishWorkflowDraft(workflowId, definitionTransport);
  const putDraftGrant = usePutWorkflowDraftGrantIntent(
    workflowId,
    definitionTransport,
  );
  const deleteDraftGrant = useDeleteWorkflowDraftGrantIntent(
    workflowId,
    definitionTransport,
  );
  const putVersionGrant = usePutWorkflowVersionGrant(
    workflowId,
    versionDetailId ?? "",
    definitionTransport,
  );
  const revokeVersionGrant = useRevokeWorkflowVersionGrant(
    workflowId,
    versionDetailId ?? "",
    definitionTransport,
  );

  const [editor, setEditor] = useState<EditorAuthority | null>(null);
  const editorRef = useRef<EditorAuthority | null>(null);
  editorRef.current = editor;
  const [validationResult, setValidationResult] =
    useState<ValidationAuthority | null>(null);
  const [publishResult, setPublishResult] =
    useState<WorkflowPublishResponseV1 | null>(null);
  const [focusTarget, setFocusTarget] =
    useState<WorkflowValidationIssueTarget | null>(null);
  const [conflict, setConflict] = useState<WorkflowDefinitionConflict | null>(
    null,
  );
  const [versionGrantConflict, setVersionGrantConflict] = useState(false);
  const [comparing, setComparing] = useState(false);
  const [operationMessage, setOperationMessage] = useState<string | null>(null);
  const operationKeys = useRef(
    createWorkflowDefinitionOperationKeys(
      createWorkflowDefinitionIdempotencyKey,
    ),
  );

  const installDraft = useCallback(
    (nextDraft: WorkflowDraftResponseV1) => {
      const next: EditorAuthority = {
        workflowId,
        draft: nextDraft,
        flushRegistry: createWorkflowEditorFlushRegistry(),
        store: createWorkflowEditorStore({
          document: workflowDraftDocument(nextDraft),
        }),
      };
      setEditor((previous) => {
        previous?.store.dispose();
        return next;
      });
      setValidationResult(null);
      setPublishResult(null);
      setFocusTarget(null);
      setConflict(null);
      setVersionGrantConflict(false);
      setSelectedVersionId(null);
      setOperationMessage(null);
      operationKeys.current.clear();
    },
    [workflowId],
  );

  useEffect(() => {
    if (draft.data === undefined || editor?.workflowId === workflowId) return;
    installDraft(draft.data);
  }, [draft.data, editor?.workflowId, installDraft, workflowId]);

  useEffect(
    () => () => {
      editorRef.current?.store.dispose();
      editorRef.current = null;
      operationKeys.current.clear();
    },
    [],
  );

  const versionItems = useMemo(() => {
    const history = uniqueVersions(versions.data?.pages ?? []);
    if (
      versionDetail.data !== undefined &&
      !history.some((item) => item.id === versionDetail.data.id)
    ) {
      return [versionDetail.data, ...history];
    }
    return history;
  }, [versionDetail.data, versions.data?.pages]);

  if (!permissions.canRead) {
    return <ProjectAccessDenied area="工作流" projectSlug={project.slug} />;
  }
  if (readiness.isPending) return <WorkflowDetailLoading />;
  if (
    readiness.isError ||
    readiness.data === undefined ||
    readiness.data.status === "unavailable"
  ) {
    return (
      <WorkflowDetailUnavailable
        onRetry={() => {
          void readiness.refetch();
        }}
      />
    );
  }
  if (!controlReady) return <WorkflowDetailUnavailable disabled />;
  if (
    catalog.isPending ||
    definition.isPending ||
    draft.isPending ||
    editor === null
  ) {
    return <WorkflowDetailLoading />;
  }
  if (
    catalog.isError ||
    definition.isError ||
    draft.isError ||
    catalog.data === undefined ||
    definition.data === undefined ||
    draft.data === undefined
  ) {
    return (
      <WorkflowDetailUnavailable
        onRetry={() => {
          void Promise.all([
            catalog.refetch(),
            definition.refetch(),
            draft.refetch(),
          ]);
        }}
      />
    );
  }

  const readOnly =
    !permissions.canEdit || definition.data.lifecycle === "archived";
  const editorMutationPending = save.isPending || publish.isPending;
  const grantBusy =
    putDraftGrant.isPending ||
    deleteDraftGrant.isPending ||
    putVersionGrant.isPending ||
    revokeVersionGrant.isPending;

  const focusIssue = (issue: WorkflowValidationIssueV1) => {
    focusWorkflowInspectorIssue(editor.store, issue, setFocusTarget);
  };

  const handleMutationError = (
    error: unknown,
    operation: "save" | "publish" | "draft_grant" | "version_grant",
    requestIdentity: string,
  ) => {
    if (definitiveFailure(error)) {
      operationKeys.current.complete(operation, requestIdentity);
    }
    if (
      error instanceof ProjectWorkflowApiError &&
      error.code === "WORKFLOW_DRAFT_CONFLICT"
    ) {
      if (operation === "version_grant") {
        setVersionGrantConflict(true);
      } else {
        setConflict(
          createWorkflowDefinitionConflict(
            editor.draft,
            editor.store.getState().current,
          ),
        );
      }
    }
    setOperationMessage(mutationMessage(error));
  };

  const putGrant = (
    target: WorkflowDefinitionGrantTarget,
    body: WorkflowCredentialGrantMutationRequestV1,
  ) => {
    setOperationMessage(null);
    setVersionGrantConflict(false);
    if (target.kind === "draft") {
      setConflict(null);
      if (!permissions.canDraftGrant) return;
      const requestIdentity = workflowDefinitionRequestIdentity({
        action: "put",
        target,
        body,
      });
      putDraftGrant.mutate(
        {
          slotId: target.slotId,
          body,
          idempotencyKey: operationKeys.current.current(
            "draft_grant",
            requestIdentity,
          ),
        },
        {
          onSuccess: () => {
            operationKeys.current.complete("draft_grant", requestIdentity);
            setOperationMessage("Draft 凭据授权意图已保存。");
          },
          onError: (error) =>
            handleMutationError(error, "draft_grant", requestIdentity),
        },
      );
      return;
    }
    if (!permissions.canVersionGrant || target.versionId !== versionDetailId) {
      return;
    }
    const requestIdentity = workflowDefinitionRequestIdentity({
      action: "put",
      target,
      body,
    });
    putVersionGrant.mutate(
      {
        slotId: target.slotId,
        body,
        idempotencyKey: operationKeys.current.current(
          "version_grant",
          requestIdentity,
        ),
      },
      {
        onSuccess: () => {
          operationKeys.current.complete("version_grant", requestIdentity);
          setOperationMessage("已发布版本的凭据授权已更新。");
          void versionDetail.refetch();
        },
        onError: (error) =>
          handleMutationError(error, "version_grant", requestIdentity),
      },
    );
  };

  const deleteGrant = (target: WorkflowDefinitionGrantTarget) => {
    setOperationMessage(null);
    setVersionGrantConflict(false);
    if (target.kind === "draft") {
      setConflict(null);
      if (!permissions.canDraftGrant) return;
      const requestIdentity = workflowDefinitionRequestIdentity({
        action: "delete",
        target,
      });
      deleteDraftGrant.mutate(
        {
          slotId: target.slotId,
          idempotencyKey: operationKeys.current.current(
            "draft_grant",
            requestIdentity,
          ),
        },
        {
          onSuccess: () => {
            operationKeys.current.complete("draft_grant", requestIdentity);
            setOperationMessage("Draft 凭据授权意图已删除。");
          },
          onError: (error) =>
            handleMutationError(error, "draft_grant", requestIdentity),
        },
      );
      return;
    }
    if (!permissions.canVersionGrant || target.versionId !== versionDetailId) {
      return;
    }
    const requestIdentity = workflowDefinitionRequestIdentity({
      action: "delete",
      target,
    });
    revokeVersionGrant.mutate(
      {
        slotId: target.slotId,
        idempotencyKey: operationKeys.current.current(
          "version_grant",
          requestIdentity,
        ),
      },
      {
        onSuccess: () => {
          operationKeys.current.complete("version_grant", requestIdentity);
          setOperationMessage("已发布版本的凭据授权已撤销。");
          void versionDetail.refetch();
        },
        onError: (error) =>
          handleMutationError(error, "version_grant", requestIdentity),
      },
    );
  };

  return (
    <main className="flex min-h-0 flex-1 flex-col">
      {conflict ? (
        <div className="p-3">
          <WorkflowDefinitionConflictBanner
            comparing={comparing}
            conflict={conflict}
            onCompare={() => {
              setComparing(true);
              void draft.refetch().then((result) => {
                setComparing(false);
                if (result.data) {
                  setConflict(
                    createWorkflowDefinitionConflict(
                      conflict.baseline,
                      conflict.local,
                      result.data,
                    ),
                  );
                }
              });
            }}
            onReload={() => {
              const reload = async () => {
                const remote =
                  conflict.remote ?? (await draft.refetch()).data ?? null;
                if (remote !== null) installDraft(remote);
              };
              void reload();
            }}
          />
        </div>
      ) : null}
      {versionGrantConflict ? (
        <div
          className="border-destructive/40 bg-destructive/5 m-3 flex items-center justify-between gap-3 rounded-md border p-3"
          data-testid="workflow-version-grant-conflict"
        >
          <p className="text-destructive text-xs" role="alert">
            已发布版本的凭据状态已变化；本地表单仍保留，请先重新读取再重试。
          </p>
          <Button
            size="sm"
            type="button"
            variant="outline"
            onClick={() => {
              setVersionGrantConflict(false);
              void versionDetail.refetch();
            }}
          >
            重新读取版本
          </Button>
        </div>
      ) : null}
      {operationMessage ? (
        <p className="border-border bg-muted px-4 py-2 text-xs" role="status">
          {operationMessage}
        </p>
      ) : null}

      <WorkflowWorkbenchFlushProvider registry={editor.flushRegistry}>
        <WorkflowWorkbench
          capabilities={project.capabilities}
          disabled={
            editorMutationPending || definition.data.lifecycle === "archived"
          }
          name={definition.data.name}
          publishing={publish.isPending}
          readOnly={readOnly}
          runDisabled
          saving={save.isPending}
          store={editor.store}
          validating={validate.isPending}
          versionLabel={`Draft r${editor.draft.revision}`}
          onBack={() =>
            router.push(
              `/projects/${encodeURIComponent(project.slug)}/workflows`,
            )
          }
          onSave={() => {
            let submitted: ReturnType<
              WorkflowEditorStore["getState"]
            >["current"];
            try {
              submitted = flushWorkflowEditorBeforeAction(
                editor.flushRegistry,
                () => editor.store.getState().current,
              );
            } catch {
              setOperationMessage(
                "节点编辑器仍有无法提交的内容；未发送保存请求，请修正后重试。",
              );
              return;
            }
            const body = workflowDraftSaveRequest(editor.draft, submitted);
            const requestIdentity = workflowDefinitionRequestIdentity(body);
            setOperationMessage(null);
            setConflict(null);
            save.mutate(
              {
                body,
                idempotencyKey: operationKeys.current.current(
                  "save",
                  requestIdentity,
                ),
              },
              {
                onSuccess: (saved) => {
                  operationKeys.current.complete("save", requestIdentity);
                  editor.store.markSaved(submitted);
                  setEditor((current) =>
                    current?.store === editor.store
                      ? { ...current, draft: saved }
                      : current,
                  );
                  setValidationResult(null);
                  setConflict(null);
                  setOperationMessage("草稿已保存。");
                },
                onError: (error) =>
                  handleMutationError(error, "save", requestIdentity),
              },
            );
          }}
          onValidate={() => {
            const attempt = runWorkflowSavedDraftAction(
              editor,
              () => editorRef.current,
              ({ draft: activeDraft, store: activeStore, submitted }) => {
                const submittedRevision = activeDraft.revision;
                const submittedChecksum = activeDraft.draft_checksum;
                setOperationMessage(null);
                setValidationResult(null);
                validate.mutate(
                  {
                    body: {
                      expected_revision: submittedRevision,
                      expected_draft_checksum: submittedChecksum,
                    },
                  },
                  {
                    onSuccess: (result) => {
                      const activeEditor = editorRef.current;
                      if (
                        activeEditor?.store !== activeStore ||
                        activeEditor.draft.revision !== submittedRevision ||
                        activeEditor.draft.draft_checksum !==
                          submittedChecksum ||
                        result.draft_revision !== submittedRevision ||
                        result.draft_checksum !== submittedChecksum ||
                        !applyWorkflowValidationIfCurrent(
                          activeStore,
                          submitted,
                          result.issues,
                        )
                      ) {
                        setOperationMessage(
                          "本地草稿在校验期间已变化；已忽略过期结果，请保存后重新校验。",
                        );
                        return;
                      }
                      setValidationResult({ result, submitted });
                      setOperationMessage(
                        result.valid
                          ? "服务端校验通过。"
                          : "服务端校验未通过。",
                      );
                      const first = result.issues[0];
                      if (first) focusIssue(first);
                    },
                    onError: (error) => {
                      setOperationMessage(mutationMessage(error));
                    },
                  },
                );
              },
            );
            if (attempt.status === "flush_failed") {
              setOperationMessage(
                "节点编辑器仍有无法提交的内容；未发送校验请求，请修正后重试。",
              );
              return;
            }
            if (attempt.status !== "submitted") {
              setValidationResult(null);
              setOperationMessage(attempt.message);
            }
          }}
          onPublish={
            permissions.canPublish
              ? () => {
                  const attempt = runWorkflowSavedDraftAction(
                    editor,
                    () => editorRef.current,
                    ({ draft: activeDraft, store: activeStore, submitted }) => {
                      const body = {
                        expected_revision: activeDraft.revision,
                        expected_draft_checksum: activeDraft.draft_checksum,
                      };
                      const requestIdentity =
                        workflowDefinitionRequestIdentity(body);
                      setOperationMessage(null);
                      setConflict(null);
                      publish.mutate(
                        {
                          body,
                          idempotencyKey: operationKeys.current.current(
                            "publish",
                            requestIdentity,
                          ),
                        },
                        {
                          onSuccess: (result) => {
                            operationKeys.current.complete(
                              "publish",
                              requestIdentity,
                            );
                            const stayedCurrent =
                              settleWorkflowPublishIfCurrent(
                                activeStore,
                                submitted,
                              );
                            setPublishResult(result);
                            setSelectedVersionId(result.version_id);
                            setOperationMessage(
                              stayedCurrent
                                ? result.executable
                                  ? "工作流版本已发布。"
                                  : "工作流版本已发布，仍需完成凭据绑定。"
                                : "工作流版本已发布；发布期间产生的本地更改仍未保存。",
                            );
                            void Promise.all([
                              definition.refetch(),
                              versions.refetch(),
                            ]);
                          },
                          onError: (error) =>
                            handleMutationError(
                              error,
                              "publish",
                              requestIdentity,
                            ),
                        },
                      );
                    },
                  );
                  if (attempt.status === "flush_failed") {
                    setOperationMessage(
                      "节点编辑器仍有无法提交的内容；未发送发布请求，请修正后重试。",
                    );
                    return;
                  }
                  if (attempt.status !== "submitted") {
                    setOperationMessage(attempt.message);
                  }
                }
              : undefined
          }
          palette={
            <WorkflowDefinitionPalette
              disabled={readOnly || editorMutationPending}
              entries={catalog.data.entries}
              locale={locale}
            />
          }
          canvas={
            <WorkflowDefinitionCanvas
              catalog={catalog.data}
              focusTarget={focusTarget}
              locale={locale}
              onCommandIssue={setOperationMessage}
              readOnly={readOnly || editorMutationPending}
            />
          }
          inspector={
            <WorkflowDefinitionInspector
              capabilities={project.capabilities}
              catalog={catalog.data}
              disabled={editorMutationPending}
              locale={locale}
              modelCatalog={modelCatalog}
              readOnly={readOnly}
              onFocusIssue={setFocusTarget}
            />
          }
          runPanel={
            <WorkflowValidationAndVersionPanel
              catalog={catalog.data}
              canDraftGrant={permissions.canDraftGrant}
              canVersionGrant={permissions.canVersionGrant}
              definition={definition.data}
              draft={editor.draft}
              grantBusy={grantBusy}
              grantVersions={versionDetail.data ? [versionDetail.data] : []}
              hasMoreVersions={Boolean(versions.hasNextPage)}
              loadingMoreVersions={versions.isFetchingNextPage}
              locale={locale}
              currentVersionStatus={
                !versionDetailExpected
                  ? "none"
                  : versionDetail.isPending
                    ? "loading"
                    : versionDetail.isError || versionDetail.data === undefined
                      ? "error"
                      : "ready"
              }
              publishResult={publishResult}
              validation={validationResult}
              versions={versionItems}
              onDeleteGrant={deleteGrant}
              onFocusIssue={focusIssue}
              onLoadMore={() => {
                if (versions.hasNextPage && !versions.isFetchingNextPage) {
                  void versions.fetchNextPage();
                }
              }}
              onRetryCurrentVersion={() => {
                void versionDetail.refetch();
              }}
              onRetryVersionHistory={() => {
                void versions.refetch();
              }}
              onSelectVersion={setSelectedVersionId}
              onPutGrant={putGrant}
              selectedVersionId={versionDetailId}
              versionHistoryStatus={
                versions.isPending
                  ? "loading"
                  : versions.isError || versions.data === undefined
                    ? "error"
                    : "ready"
              }
            />
          }
        />
      </WorkflowWorkbenchFlushProvider>
    </main>
  );
}
