"use client";

import {
  ArrowLeftIcon,
  CheckCircle2Icon,
  CirclePlayIcon,
  Redo2Icon,
  RocketIcon,
  SaveIcon,
  Undo2Icon,
} from "lucide-react";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useCallback,
  useId,
} from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import type { NodeCatalogEntry } from "@/core/project-workflows/catalog";
import type { WorkflowDraftPortLocale } from "@/core/project-workflows/editor/ports";
import type { WorkflowEditorCommandResult } from "@/core/project-workflows/editor/store";
import type { WorkflowNodeKind } from "@/core/project-workflows/types";
import type { Capability } from "@/core/projects/types";
import { cn } from "@/lib/utils";

import {
  updateWorkflowEditorSession,
  useWorkflowWorkbenchSnapshot,
  useWorkflowWorkbenchStore,
  WorkflowWorkbenchStoreProvider,
  type WorkflowWorkbenchAddNextStepCommand,
  type WorkflowWorkbenchStorePort,
} from "./workbench-store-context";

const INSPECTOR_MIN_WIDTH = 400;
const INSPECTOR_MAX_WIDTH = 600;

export type WorkflowNextStepCandidate = {
  nodeType: WorkflowNodeKind;
  targetPortId: string;
  title: string;
};

export type WorkflowNextStepSourcePort = {
  id: string;
  kind: "control" | "data";
  cardinality: "one" | "many";
  label?: string;
  title_i18n?: { "zh-CN": string; "en-US": string };
};

export type WorkflowNextStepFilterInput = {
  capabilities: readonly Capability[];
  catalog: readonly NodeCatalogEntry[];
  document: WorkflowPersistedDocumentV1;
  sourceNodeId: string;
  sourceNodeType: string | null | undefined;
  sourcePort: WorkflowNextStepSourcePort;
  locale?: WorkflowDraftPortLocale;
};

export type WorkflowNextStepCommandFactory = (input: {
  sourceNodeId: string;
  sourcePortId: string;
  candidate: WorkflowNextStepCandidate;
  document: ReturnType<WorkflowWorkbenchStorePort["getState"]>["current"];
}) => WorkflowWorkbenchAddNextStepCommand | null;

export function filterWorkflowNextStepCandidates({
  capabilities,
  catalog,
  document,
  sourceNodeId,
  sourceNodeType,
  sourcePort,
  locale = "zh-CN",
}: WorkflowNextStepFilterInput): WorkflowNextStepCandidate[] {
  if (
    sourceNodeType === "end" ||
    sourcePort.kind !== "control" ||
    !capabilities.includes("workflow.edit")
  ) {
    return [];
  }

  const sourceNode = (document.spec.nodes ?? []).find(
    (node) => node.id === sourceNodeId,
  );
  const sourceScope =
    sourceNode?.scope && typeof sourceNode.scope === "object"
      ? (sourceNode.scope as { kind?: unknown; loop_node_id?: unknown })
      : null;
  const insideLoop =
    sourcePort.id === "body" || sourceScope?.kind === "loop_body";
  if (sourcePort.id === "body" && sourceNodeType !== "loop") return [];
  if (
    sourceNodeType === "loop" &&
    sourcePort.id === "body" &&
    sourceNode?.config &&
    typeof sourceNode.config === "object" &&
    "body_entry_node_id" in sourceNode.config &&
    typeof sourceNode.config.body_entry_node_id === "string"
  ) {
    return [];
  }

  const outgoingCount = (document.spec.transitions ?? []).filter(
    (transition) =>
      transition.source?.node_id === sourceNodeId &&
      transition.source.port_id === sourcePort.id,
  ).length;
  if (sourcePort.cardinality === "one" && outgoingCount >= 1) return [];

  const granted = new Set<string>(capabilities);
  const candidates: WorkflowNextStepCandidate[] = [];

  for (const entry of catalog) {
    if (entry.availability.state !== "enabled") continue;
    if (entry.definition.type === "start") continue;
    if (
      insideLoop &&
      (entry.definition.type === "end" || entry.definition.type === "loop")
    ) {
      continue;
    }
    if (
      !entry.definition.required_capabilities.every((capability) =>
        granted.has(capability),
      )
    ) {
      continue;
    }

    const targetPort = entry.definition.input_ports.find(
      (port) => port.kind === "control",
    );
    if (!targetPort) continue;

    candidates.push({
      nodeType: entry.definition.type,
      targetPortId: targetPort.id,
      title:
        entry.definition.title_i18n[locale] ??
        entry.definition.title_i18n["zh-CN"] ??
        entry.definition.type,
    });
  }

  return candidates;
}

export function commitWorkflowNextStep(
  store: WorkflowWorkbenchStorePort,
  command: WorkflowWorkbenchAddNextStepCommand,
): WorkflowEditorCommandResult {
  return store.dispatch(command);
}

export type WorkflowHistoryShortcutInput = {
  altKey: boolean;
  ctrlKey: boolean;
  editingTarget?: boolean;
  isComposing?: boolean;
  key: string;
  metaKey: boolean;
  shiftKey: boolean;
};

export function workflowHistoryShortcutAction({
  altKey,
  ctrlKey,
  editingTarget = false,
  isComposing = false,
  key,
  metaKey,
  shiftKey,
}: WorkflowHistoryShortcutInput): "redo" | "undo" | null {
  if (
    editingTarget ||
    isComposing ||
    altKey ||
    (!metaKey && !ctrlKey) ||
    key.toLowerCase() !== "z"
  ) {
    return null;
  }
  return shiftKey ? "redo" : "undo";
}

function isWorkflowTextEditingTarget(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    target.closest(
      'input, textarea, select, [contenteditable]:not([contenteditable="false"]), .cm-editor, .cm-content, [data-codemirror]',
    ) !== null
  );
}

function handleWorkflowHistoryShortcut(
  event: ReactKeyboardEvent<HTMLElement>,
  store: WorkflowWorkbenchStorePort,
  editable: boolean,
): void {
  if (!editable || event.defaultPrevented) return;
  const action = workflowHistoryShortcutAction({
    altKey: event.altKey,
    ctrlKey: event.ctrlKey,
    editingTarget: isWorkflowTextEditingTarget(event.target),
    isComposing: event.nativeEvent.isComposing,
    key: event.key,
    metaKey: event.metaKey,
    shiftKey: event.shiftKey,
  });
  if (action === null) return;
  const history = store.getState().history;
  if (
    (action === "undo" && history.past.length === 0) ||
    (action === "redo" && history.future.length === 0)
  ) {
    return;
  }
  event.preventDefault();
  if (action === "undo") store.undo();
  else store.redo();
}

export function clampWorkflowInspectorWidth(width: number): number {
  if (!Number.isFinite(width)) return 480;
  return Math.min(
    INSPECTOR_MAX_WIDTH,
    Math.max(INSPECTOR_MIN_WIDTH, Math.round(width)),
  );
}

export function updateWorkflowInspectorWidth(
  store: WorkflowWorkbenchStorePort,
  width: number,
): void {
  const widthPx = clampWorkflowInspectorWidth(width);
  updateWorkflowEditorSession(store, (session) => {
    if (session.inspector.width_px === widthPx) return session;
    return {
      ...session,
      inspector: { ...session.inspector, width_px: widthPx },
    };
  });
}

export type WorkflowWorkbenchProps = {
  store: WorkflowWorkbenchStorePort;
  name: string;
  versionLabel?: string;
  capabilities: readonly Capability[];
  readOnly?: boolean;
  disabled?: boolean;
  validating?: boolean;
  publishing?: boolean;
  saving?: boolean;
  running?: boolean;
  runDisabled?: boolean;
  palette?: ReactNode;
  canvas?: ReactNode;
  inspector?: ReactNode;
  runPanel?: ReactNode;
  onBack?: () => void;
  onSave?: () => void;
  onValidate?: () => void;
  onPublish?: () => void;
  onRun?: () => void;
  className?: string;
};

function WorkflowWorkbenchHeader({
  capabilities,
  disabled = false,
  name,
  onBack,
  onPublish,
  onRun,
  onSave,
  onValidate,
  publishing = false,
  readOnly = false,
  runDisabled = false,
  running = false,
  saving = false,
  validating = false,
  versionLabel,
}: Pick<
  WorkflowWorkbenchProps,
  | "capabilities"
  | "disabled"
  | "name"
  | "onBack"
  | "onPublish"
  | "onRun"
  | "onSave"
  | "onValidate"
  | "publishing"
  | "readOnly"
  | "runDisabled"
  | "running"
  | "saving"
  | "validating"
  | "versionLabel"
>) {
  const store = useWorkflowWorkbenchStore();
  const snapshot = useWorkflowWorkbenchSnapshot();
  const granted = new Set<Capability>(capabilities);
  const editable = !disabled && !readOnly && granted.has("workflow.edit");
  const canUndo = editable && snapshot.history.past.length > 0;
  const canRedo = editable && snapshot.history.future.length > 0;
  const canSave = editable && snapshot.dirty && Boolean(onSave) && !saving;
  const canValidate =
    editable && !snapshot.dirty && Boolean(onValidate) && !validating;
  const canPublish =
    !disabled &&
    !snapshot.dirty &&
    granted.has("workflow.publish") &&
    Boolean(onPublish) &&
    !publishing;
  const canRun =
    !disabled &&
    granted.has("workflow.execute") &&
    Boolean(onRun) &&
    !running &&
    !runDisabled &&
    !snapshot.dirty;
  const errorCount = snapshot.validationIssues.filter(
    (issue) => issue.severity === "error",
  ).length;

  return (
    <header
      data-slot="workflow-workbench-header"
      className="border-border bg-background flex min-h-14 items-center gap-2 border-b px-3 py-2"
    >
      {onBack ? (
        <Button
          aria-label="返回工作流列表"
          title="返回工作流列表"
          size="icon-sm"
          variant="ghost"
          onClick={onBack}
        >
          <ArrowLeftIcon />
        </Button>
      ) : null}

      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <h1 className="truncate text-sm font-semibold">{name}</h1>
          {versionLabel ? (
            <Badge variant="outline">{versionLabel}</Badge>
          ) : null}
        </div>
        <div className="text-muted-foreground flex items-center gap-2 text-xs">
          <span
            aria-live="polite"
            data-save-state={snapshot.dirty ? "dirty" : "saved"}
            role="status"
          >
            {snapshot.dirty ? "未保存" : "已保存"}
          </span>
          {errorCount > 0 ? (
            <span role="status">{errorCount} 个校验错误</span>
          ) : null}
          {readOnly ? <span>只读</span> : null}
        </div>
      </div>

      <div
        aria-label="编辑历史"
        className="flex items-center gap-1"
        role="group"
      >
        <Button
          aria-keyshortcuts="Meta+Z Control+Z"
          aria-label="撤销"
          disabled={!canUndo}
          size="icon-sm"
          title="撤销"
          variant="ghost"
          onClick={() => store.undo()}
        >
          <Undo2Icon />
        </Button>
        <Button
          aria-keyshortcuts="Meta+Shift+Z Control+Shift+Z"
          aria-label="重做"
          disabled={!canRedo}
          size="icon-sm"
          title="重做"
          variant="ghost"
          onClick={() => store.redo()}
        >
          <Redo2Icon />
        </Button>
      </div>

      <div
        aria-label="工作流操作"
        className="flex items-center gap-1"
        role="group"
      >
        <Button
          aria-label="保存草稿"
          disabled={!canSave}
          size="sm"
          title={
            !editable
              ? "需要工作流编辑权限"
              : snapshot.dirty
                ? "保存草稿"
                : "草稿已保存"
          }
          variant="outline"
          onClick={onSave}
        >
          <SaveIcon />
          {saving ? "保存中" : "保存"}
        </Button>
        <Button
          aria-label="校验工作流"
          disabled={!canValidate}
          size="sm"
          title={
            !editable
              ? "需要工作流编辑权限"
              : snapshot.dirty
                ? "请先保存当前更改"
                : "校验工作流"
          }
          variant="outline"
          onClick={onValidate}
        >
          <CheckCircle2Icon />
          {validating ? "校验中" : "校验"}
        </Button>
        <Button
          aria-label="发布工作流"
          disabled={!canPublish}
          size="sm"
          title={
            !granted.has("workflow.publish")
              ? "需要工作流发布权限"
              : snapshot.dirty
                ? "请先保存当前更改"
                : "发布工作流"
          }
          variant="outline"
          onClick={onPublish}
        >
          <RocketIcon />
          {publishing ? "发布中" : "发布"}
        </Button>
        <Button
          aria-label="运行工作流"
          disabled={!canRun}
          size="sm"
          title={
            snapshot.dirty
              ? "请先保存当前更改"
              : !granted.has("workflow.execute")
                ? "需要工作流运行权限"
                : "运行已发布工作流"
          }
          onClick={onRun}
        >
          <CirclePlayIcon />
          {running ? "运行中" : "运行"}
        </Button>
      </div>
    </header>
  );
}

function WorkflowWorkbenchFrame(props: Omit<WorkflowWorkbenchProps, "store">) {
  const { canvas, className, inspector, palette, runPanel, ...headerProps } =
    props;
  const panelId = useId();
  const store = useWorkflowWorkbenchStore();
  const snapshot = useWorkflowWorkbenchSnapshot();
  const editable =
    !props.disabled &&
    !props.readOnly &&
    props.capabilities.includes("workflow.edit");
  const inspectorOpen =
    snapshot.editorSession.inspector.open && Boolean(inspector);
  const inspectorWidth = clampWorkflowInspectorWidth(
    snapshot.editorSession.inspector.width_px,
  );
  const handleInspectorResize = useCallback(
    (size: { inPixels: number }) => {
      updateWorkflowInspectorWidth(store, size.inPixels);
    },
    [store],
  );

  return (
    <section
      aria-label={`${props.name} 工作流编辑器`}
      className={cn(
        "bg-muted/20 flex h-full min-h-[36rem] w-full flex-col overflow-hidden",
        className,
      )}
      data-slot="workflow-workbench"
      onKeyDown={(event) =>
        handleWorkflowHistoryShortcut(event, store, editable)
      }
    >
      <WorkflowWorkbenchHeader {...headerProps} />

      <div className="min-h-0 flex-1">
        <ResizablePanelGroup id={`${panelId}-vertical`} orientation="vertical">
          <ResizablePanel
            defaultSize="75%"
            id={`${panelId}-workspace`}
            minSize={320}
          >
            <ResizablePanelGroup
              id={`${panelId}-horizontal`}
              orientation="horizontal"
            >
              <ResizablePanel
                defaultSize={248}
                id={`${panelId}-palette`}
                maxSize={360}
                minSize={220}
              >
                <aside
                  aria-label="节点目录"
                  className="border-border bg-background h-full overflow-auto border-r"
                  data-slot="workflow-workbench-palette"
                >
                  {palette ?? (
                    <p className="text-muted-foreground p-4 text-sm">
                      节点目录
                    </p>
                  )}
                </aside>
              </ResizablePanel>
              <ResizableHandle withHandle />
              <ResizablePanel id={`${panelId}-canvas`} minSize={320}>
                <main
                  aria-label="工作流画布"
                  className="bg-background relative h-full overflow-hidden"
                  data-slot="workflow-workbench-canvas"
                >
                  {canvas ?? (
                    <p className="text-muted-foreground p-4 text-sm">
                      流程画布
                    </p>
                  )}
                </main>
              </ResizablePanel>

              {inspectorOpen ? (
                <>
                  <ResizableHandle withHandle />
                  <ResizablePanel
                    defaultSize={inspectorWidth}
                    id={`${panelId}-inspector`}
                    maxSize={INSPECTOR_MAX_WIDTH}
                    minSize={INSPECTOR_MIN_WIDTH}
                    onResize={handleInspectorResize}
                  >
                    <aside
                      aria-label="节点检查器"
                      className="border-border bg-background h-full border-l"
                      data-slot="workflow-workbench-inspector"
                    >
                      {inspector}
                    </aside>
                  </ResizablePanel>
                </>
              ) : null}
            </ResizablePanelGroup>
          </ResizablePanel>

          <ResizableHandle withHandle />
          <ResizablePanel
            defaultSize="25%"
            id={`${panelId}-run-panel`}
            maxSize="45%"
            minSize={140}
          >
            <section
              aria-label="运行面板"
              className="border-border bg-background h-full overflow-auto border-t"
              data-slot="workflow-workbench-run-panel"
            >
              {runPanel ?? (
                <p className="text-muted-foreground p-4 text-sm">
                  选择一次运行以查看时间线与安全预览。
                </p>
              )}
            </section>
          </ResizablePanel>
        </ResizablePanelGroup>
      </div>
    </section>
  );
}

export function WorkflowWorkbench({ store, ...props }: WorkflowWorkbenchProps) {
  return (
    <WorkflowWorkbenchStoreProvider store={store}>
      <WorkflowWorkbenchFrame {...props} />
    </WorkflowWorkbenchStoreProvider>
  );
}

export {
  useWorkflowWorkbenchSnapshot,
  useWorkflowWorkbenchStore,
  WorkflowWorkbenchStoreProvider,
  type WorkflowWorkbenchCommand,
  type WorkflowWorkbenchAddNextStepCommand,
  type WorkflowWorkbenchStorePort,
  type WorkflowWorkbenchStoreSnapshot,
} from "./workbench-store-context";
