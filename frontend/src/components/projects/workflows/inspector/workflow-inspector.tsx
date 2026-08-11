"use client";

import {
  BookOpenIcon,
  BoxesIcon,
  ChevronDownIcon,
  MoreHorizontalIcon,
  XIcon,
} from "lucide-react";
import {
  createContext,
  type ReactNode,
  type UIEvent,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { type WorkflowModelCatalogProjection } from "@/components/projects/workflows/node-config/contracts";
import { WORKFLOW_NODE_CONFIG_PANELS } from "@/components/projects/workflows/node-config/registry";
import { WorkflowNodeConfig } from "@/components/projects/workflows/node-config/workflow-node-config";
import {
  updateWorkflowEditorSession,
  useWorkflowWorkbenchSnapshot,
  useWorkflowWorkbenchStore,
  type WorkflowWorkbenchStorePort,
} from "@/components/projects/workflows/workbench/workbench-store-context";
import {
  commitWorkflowNextStep,
  filterWorkflowNextStepCandidates,
  type WorkflowNextStepCommandFactory,
  type WorkflowNextStepCandidate,
  type WorkflowNextStepSourcePort,
} from "@/components/projects/workflows/workbench/workflow-workbench";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { type WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import {
  firstBatchNodeTitles,
  type NodeCatalogEntry,
} from "@/core/project-workflows/catalog";
import type { WorkflowDraftPortLocale } from "@/core/project-workflows/editor/ports";
import {
  workflowValidationIssueTarget,
  type WorkflowValidationIssueTarget,
} from "@/core/project-workflows/editor/validation";
import type {
  SafePreviewV1,
  WorkflowNodeLastRunV1,
  WorkflowValidationIssueV1,
} from "@/core/project-workflows/transport";
import {
  workflowNodeKindSchema,
  type WorkflowNodeKind,
} from "@/core/project-workflows/types";
import type { Capability } from "@/core/projects/types";
import { cn } from "@/lib/utils";

type WorkflowDraftNode = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["nodes"]
>[number];

type WorkflowInspectorNode = {
  draft: WorkflowDraftNode;
  id: string;
  rawType: string | null;
  nodeType: WorkflowNodeKind | null;
  customLabel: string;
  description: string;
  identityIncomplete: boolean;
  structurallyIncomplete: boolean;
  unsupportedVersion: boolean;
};

const CANONICAL_NODE_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const WorkflowInspectorSettingsDisabledContext = createContext(false);

export type WorkflowInspectorProps = {
  capabilities: readonly Capability[];
  catalog: readonly NodeCatalogEntry[];
  sourcePorts: readonly WorkflowNextStepSourcePort[];
  lastRun?: WorkflowNodeLastRunV1 | null;
  readOnly?: boolean;
  disabled?: boolean;
  docsHref?: string;
  presentation?: "panel" | "sheet";
  headerActions?: ReactNode;
  children?: ReactNode;
  locale?: WorkflowDraftPortLocale;
  modelCatalog?: WorkflowModelCatalogProjection;
  onFocusIssue?: (target: WorkflowValidationIssueTarget) => void;
  createNextStepCommand?: WorkflowNextStepCommandFactory;
};

export type WorkflowInspectorSectionProps = {
  id: string;
  title: string;
  required?: boolean;
  help?: ReactNode;
  error?: string | null;
  children?: ReactNode;
  className?: string;
};

function selectedInspectorNode(
  document: WorkflowPersistedDocumentV1,
  nodeId: string | null,
): WorkflowInspectorNode | null {
  if (!nodeId) return null;
  const node = (document.spec.nodes ?? []).find(
    (candidate) => candidate.id === nodeId,
  );
  if (node?.id !== nodeId) return null;

  const parsedType = workflowNodeKindSchema.safeParse(node.type);
  const versionMissing =
    node.type_version === null || node.type_version === undefined;
  const unsupportedVersion = !versionMissing && node.type_version !== 1;
  const canonicalIdentity = CANONICAL_NODE_ID.test(nodeId);
  const supportedPartial =
    canonicalIdentity && parsedType.success && node.type_version === 1;
  return {
    draft:
      supportedPartial && node.config == null ? { ...node, config: {} } : node,
    id: nodeId,
    rawType: typeof node.type === "string" ? node.type : null,
    nodeType: parsedType.success ? parsedType.data : null,
    customLabel: typeof node.custom_label === "string" ? node.custom_label : "",
    description: typeof node.description === "string" ? node.description : "",
    identityIncomplete:
      !canonicalIdentity || (parsedType.success && versionMissing),
    structurallyIncomplete:
      !canonicalIdentity ||
      (parsedType.success && versionMissing) ||
      node.config == null,
    unsupportedVersion,
  };
}

function issuesForNode(
  issues: readonly WorkflowValidationIssueV1[],
  nodeId: string,
): WorkflowValidationIssueV1[] {
  return issues.filter((issue) => issue.node_id === nodeId);
}

export function commitWorkflowNodePresentation(
  store: WorkflowWorkbenchStorePort,
  node: { customLabel: string; description: string; id: string },
  customLabel: string,
) {
  return store.dispatch({
    type: "update_node_presentation",
    node_id: node.id,
    custom_label: customLabel.length > 0 ? customLabel : null,
    description: node.description.length > 0 ? node.description : null,
  });
}

export function focusWorkflowInspectorIssue(
  store: WorkflowWorkbenchStorePort,
  issue: WorkflowValidationIssueV1,
  onFocusIssue?: (target: WorkflowValidationIssueTarget) => void,
): WorkflowValidationIssueTarget {
  const target = workflowValidationIssueTarget(issue);
  const nodeId =
    target.kind === "node" || target.kind === "port"
      ? target.node_id
      : target.kind === "edge"
        ? target.node_id
        : undefined;
  const edgeId =
    target.kind === "edge"
      ? target.edge_id
      : target.kind === "port"
        ? target.edge_id
        : undefined;
  updateWorkflowEditorSession(store, (session) => ({
    ...session,
    selection: {
      node_ids: nodeId ? [nodeId] : [],
      edge_ids: edgeId ? [edgeId] : [],
    },
    inspector: nodeId
      ? { ...session.inspector, open: true, node_id: nodeId }
      : session.inspector,
  }));
  onFocusIssue?.(target);
  return target;
}

export function WorkflowInspectorSection({
  children,
  className,
  error,
  help,
  id,
  required = false,
  title,
}: WorkflowInspectorSectionProps) {
  const store = useWorkflowWorkbenchStore();
  const snapshot = useWorkflowWorkbenchSnapshot();
  const settingsDisabled = useContext(WorkflowInspectorSettingsDisabledContext);
  const instanceId = useId();
  const expanded =
    snapshot.editorSession.inspector.expanded_section_ids.includes(id);
  const errorId = `${instanceId}-${id}-section-error`;

  const setExpanded = (open: boolean) => {
    updateWorkflowEditorSession(store, (session) => {
      const previous = session.inspector.expanded_section_ids;
      const expandedSectionIds = open
        ? previous.includes(id)
          ? previous
          : [...previous, id]
        : previous.filter((sectionId) => sectionId !== id);
      if (expandedSectionIds === previous) return session;
      return {
        ...session,
        inspector: {
          ...session.inspector,
          expanded_section_ids: expandedSectionIds,
        },
      };
    });
  };

  return (
    <Collapsible
      className={cn("group border-border border-b", className)}
      onOpenChange={setExpanded}
      open={expanded}
    >
      <CollapsibleTrigger
        aria-describedby={error ? errorId : undefined}
        className="focus-visible:ring-ring flex min-h-12 w-full items-center gap-2 px-4 py-3 text-left focus-visible:ring-2 focus-visible:outline-none"
      >
        <ChevronDownIcon
          aria-hidden="true"
          className="text-muted-foreground size-4 transition-transform group-data-[state=closed]:-rotate-90"
        />
        <span className="flex-1 text-sm font-medium">{title}</span>
        {required ? (
          <span className="text-destructive text-xs">必填</span>
        ) : null}
        {error ? <Badge variant="destructive">有错误</Badge> : null}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="space-y-3 px-4 pb-4">
          {help ? (
            <p className="text-muted-foreground text-xs">{help}</p>
          ) : null}
          {error ? (
            <p className="text-destructive text-xs" id={errorId} role="alert">
              {error}
            </p>
          ) : null}
          <fieldset className="m-0 border-0 p-0" disabled={settingsDisabled}>
            <legend className="sr-only">{title}配置</legend>
            {children}
          </fieldset>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function SafePreview({
  label,
  preview,
}: {
  label: string;
  preview: SafePreviewV1;
}) {
  return (
    <section
      aria-label={label}
      className="border-border space-y-2 rounded-md border p-3"
    >
      <div className="flex items-center gap-2">
        <h4 className="text-xs font-medium">{label}</h4>
        <Badge variant="outline">{preview.format.toUpperCase()}</Badge>
        {preview.redacted ? <Badge variant="secondary">已脱敏</Badge> : null}
        {preview.truncated ? <Badge variant="secondary">已截断</Badge> : null}
      </div>
      <pre className="bg-muted max-h-56 overflow-auto rounded p-3 text-xs break-words whitespace-pre-wrap">
        {preview.text}
      </pre>
      {preview.original_byte_count !== null &&
      preview.original_byte_count !== undefined ? (
        <p className="text-muted-foreground text-xs">
          原始大小：{preview.original_byte_count} bytes
        </p>
      ) : null}
    </section>
  );
}

const LAST_RUN_STATUS_LABEL: Record<WorkflowNodeLastRunV1["status"], string> = {
  queued: "等待运行",
  provisioning: "准备运行环境",
  running: "运行中",
  collecting: "收集结果",
  cleanup_pending: "等待清理",
  succeeded: "运行成功",
  failed: "运行失败",
  timed_out: "运行超时",
  cancelled: "已取消",
};

function WorkflowInspectorLastRun({
  lastRun,
  nodeId,
}: {
  lastRun?: WorkflowNodeLastRunV1 | null;
  nodeId: string;
}) {
  if (lastRun?.node_id !== nodeId) {
    return (
      <div className="text-muted-foreground p-4 text-sm" role="status">
        暂无该节点的运行记录。
      </div>
    );
  }

  return (
    <div className="space-y-3 p-4">
      <section aria-label="运行状态" className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            variant={
              lastRun.status === "failed" || lastRun.status === "timed_out"
                ? "destructive"
                : "secondary"
            }
          >
            {LAST_RUN_STATUS_LABEL[lastRun.status]}
          </Badge>
          <span className="text-muted-foreground text-xs">
            第 {lastRun.attempt} 次尝试
          </span>
          {lastRun.iteration_path.length > 0 ? (
            <span className="text-muted-foreground text-xs">
              循环 {lastRun.iteration_path.join(" / ")}
            </span>
          ) : null}
        </div>
        {lastRun.duration_ms !== null && lastRun.duration_ms !== undefined ? (
          <p className="text-muted-foreground text-xs">
            耗时 {lastRun.duration_ms} ms
          </p>
        ) : null}
      </section>

      {lastRun.input_preview ? (
        <SafePreview label="安全输入预览" preview={lastRun.input_preview} />
      ) : null}
      {lastRun.output_preview ? (
        <SafePreview label="安全输出预览" preview={lastRun.output_preview} />
      ) : null}
      {lastRun.error ? (
        <section
          aria-label="安全错误摘要"
          className="border-destructive/40 bg-destructive/5 rounded-md border p-3"
          role="alert"
        >
          <p className="font-mono text-xs">{lastRun.error.code}</p>
          <p className="mt-1 text-sm">{lastRun.error.safe_message}</p>
          {lastRun.error.line !== null && lastRun.error.line !== undefined ? (
            <p className="text-muted-foreground mt-1 text-xs">
              行 {lastRun.error.line}
              {lastRun.error.column !== null &&
              lastRun.error.column !== undefined
                ? `，列 ${lastRun.error.column}`
                : ""}
            </p>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

function WorkflowNextStep({
  capabilities,
  catalog,
  createNextStepCommand,
  disabled,
  locale,
  node,
  sourcePort,
}: {
  capabilities: readonly Capability[];
  catalog: readonly NodeCatalogEntry[];
  createNextStepCommand?: WorkflowNextStepCommandFactory;
  disabled: boolean;
  locale: WorkflowDraftPortLocale;
  node: WorkflowInspectorNode;
  sourcePort: WorkflowNextStepSourcePort;
}) {
  const store = useWorkflowWorkbenchStore();
  const snapshot = useWorkflowWorkbenchSnapshot();
  const candidates =
    disabled || !createNextStepCommand
      ? []
      : filterWorkflowNextStepCandidates({
          capabilities,
          catalog,
          document: snapshot.current,
          locale,
          sourceNodeId: node.id,
          sourceNodeType: node.nodeType,
          sourcePort,
        });
  const anchor = snapshot.editorSession.palette.anchor;
  const open =
    snapshot.editorSession.palette.open &&
    anchor?.node_id === node.id &&
    anchor.port_id === sourcePort.id;

  if (node.nodeType === "end" || sourcePort.kind !== "control") return null;

  const setOpen = (nextOpen: boolean) => {
    updateWorkflowEditorSession(store, (session) => ({
      ...session,
      palette: {
        open: nextOpen,
        anchor: nextOpen ? { node_id: node.id, port_id: sourcePort.id } : null,
      },
    }));
  };

  const selectCandidate = (candidate: WorkflowNextStepCandidate) => {
    const command = createNextStepCommand?.({
      sourceNodeId: node.id,
      sourcePortId: sourcePort.id,
      candidate,
      document: snapshot.current,
    });
    if (!command) return;
    const result = commitWorkflowNextStep(store, command);
    if (result.applied) setOpen(false);
  };

  return (
    <section
      className="border-border border-t p-4"
      data-slot="workflow-next-step"
    >
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium">下一步</p>
          <p className="text-muted-foreground text-xs">
            从“
            {sourcePort.label ??
              sourcePort.title_i18n?.["zh-CN"] ??
              sourcePort.title_i18n?.["en-US"] ??
              sourcePort.id}
            ”连接
          </p>
        </div>
        <Button
          aria-expanded={open}
          aria-haspopup="listbox"
          disabled={candidates.length === 0}
          size="sm"
          title={
            candidates.length === 0
              ? "没有符合权限、可用性和端口要求的节点"
              : "选择下一步节点"
          }
          variant="outline"
          onClick={() => setOpen(!open)}
        >
          选择节点
        </Button>
      </div>

      {open ? (
        <div
          aria-label="可添加的下一步节点"
          className="mt-3 grid gap-1"
          role="listbox"
        >
          {candidates.map((candidate) => (
            <Button
              data-slot="workflow-next-step-candidate"
              key={`${candidate.nodeType}:${candidate.targetPortId}`}
              role="option"
              variant="ghost"
              onClick={() => selectCandidate(candidate)}
            >
              {candidate.title}
            </Button>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function WorkflowInspectorBody({
  capabilities,
  catalog,
  children,
  createNextStepCommand,
  disabled = false,
  docsHref,
  headerActions,
  lastRun,
  locale = "zh-CN",
  modelCatalog,
  onFocusIssue,
  readOnly = false,
  sourcePorts,
}: Omit<WorkflowInspectorProps, "presentation">) {
  const titleId = useId();
  const store = useWorkflowWorkbenchStore();
  const snapshot = useWorkflowWorkbenchSnapshot();
  const session = snapshot.editorSession;
  const settingsScrollContainerRef = useRef<HTMLDivElement>(null);
  const node = selectedInspectorNode(
    snapshot.current,
    session.inspector.node_id,
  );
  const [customLabel, setCustomLabel] = useState(node?.customLabel ?? "");

  useEffect(() => {
    setCustomLabel(node?.customLabel ?? "");
  }, [node?.customLabel, node?.id]);

  useLayoutEffect(() => {
    if (session.inspector.tab !== "settings") return;
    const viewport =
      settingsScrollContainerRef.current?.querySelector<HTMLElement>(
        '[data-slot="scroll-area-viewport"]',
      );
    if (viewport) viewport.scrollTop = session.inspector.scroll_top;
  }, [node?.id, session.inspector.scroll_top, session.inspector.tab]);

  if (!node) {
    return (
      <div
        className="text-muted-foreground flex h-full items-center justify-center p-6 text-center text-sm"
        data-inspector-width={session.inspector.width_px}
        role="status"
      >
        选择一个节点以查看设置和上次运行。
      </div>
    );
  }

  const nodeIssues = issuesForNode(snapshot.validationIssues, node.id);
  const errorIssues = nodeIssues.filter((issue) => issue.severity === "error");
  const catalogEntry = node.nodeType
    ? catalog.find((entry) => entry.definition.type === node.nodeType)
    : undefined;
  const catalogAvailable = catalogEntry?.availability.state === "enabled";
  const unsupported = node.nodeType === null || node.unsupportedVersion;
  const incomplete = node.structurallyIncomplete || errorIssues.length > 0;
  const canEdit = capabilities.includes("workflow.edit");
  const settingsDisabled =
    disabled ||
    readOnly ||
    !canEdit ||
    unsupported ||
    !catalogAvailable ||
    node.identityIncomplete;
  const nodeTitle = unsupported
    ? "不支持的节点类型"
    : (catalogEntry?.definition.title_i18n[locale] ??
      firstBatchNodeTitles[node.nodeType!][locale] ??
      node.nodeType!);

  const close = () => {
    updateWorkflowEditorSession(store, (previous) => ({
      ...previous,
      inspector: {
        ...previous.inspector,
        open: false,
        node_id: null,
      },
      palette: { open: false, anchor: null },
    }));
  };

  const changeTab = (value: string) => {
    if (value !== "settings" && value !== "last_run") return;
    updateWorkflowEditorSession(store, (previous) => ({
      ...previous,
      inspector: { ...previous.inspector, tab: value },
    }));
  };

  const rememberScroll = (event: UIEvent<HTMLDivElement>) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const scrollTop = Math.max(0, target.scrollTop);
    updateWorkflowEditorSession(store, (previous) => {
      if (previous.inspector.scroll_top === scrollTop) return previous;
      return {
        ...previous,
        inspector: { ...previous.inspector, scroll_top: scrollTop },
      };
    });
  };

  const commitCustomLabel = () => {
    if (settingsDisabled || customLabel === node.customLabel) return;
    const result = commitWorkflowNodePresentation(store, node, customLabel);
    if (!result.applied) setCustomLabel(node.customLabel);
  };

  return (
    <div
      aria-labelledby={titleId}
      className="flex h-full min-h-0 flex-col"
      data-inspector-width={session.inspector.width_px}
      data-slot="workflow-inspector"
    >
      <header className="border-border flex items-start gap-3 border-b p-4">
        <div
          aria-hidden="true"
          className="bg-muted flex size-9 shrink-0 items-center justify-center rounded-md"
        >
          <BoxesIcon className="size-4" />
        </div>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold" id={titleId}>
              {nodeTitle}
            </h2>
            {incomplete ? (
              <Badge variant="destructive">配置不完整</Badge>
            ) : null}
            {!catalogAvailable && !unsupported ? (
              <Badge variant="outline">当前不可用</Badge>
            ) : null}
            {settingsDisabled ? <Badge variant="secondary">只读</Badge> : null}
          </div>
          <Input
            aria-label="节点实例名称"
            disabled={settingsDisabled}
            maxLength={255}
            value={customLabel}
            onBlur={commitCustomLabel}
            onChange={(event) => setCustomLabel(event.target.value)}
          />
          {node.description ? (
            <p className="text-muted-foreground text-xs">{node.description}</p>
          ) : null}
          {unsupported ? (
            <p className="text-destructive text-xs" role="alert">
              {node.unsupportedVersion
                ? `节点类型“${node.rawType ?? "未指定"}”的版本暂不受支持，已按只读方式打开。`
                : `未知节点类型“${node.rawType ?? "未指定"}”已按只读方式打开。`}
            </p>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {docsHref ? (
            <Button
              aria-label="查看节点文档"
              asChild
              size="icon-sm"
              variant="ghost"
            >
              <a href={docsHref} rel="noreferrer" target="_blank">
                <BookOpenIcon />
              </a>
            </Button>
          ) : null}
          {headerActions ? (
            <div aria-label="更多节点操作">
              <span className="sr-only">更多节点操作</span>
              <MoreHorizontalIcon className="size-4" />
              {headerActions}
            </div>
          ) : null}
          <Button
            aria-label="关闭节点检查器"
            size="icon-sm"
            title="关闭节点检查器"
            variant="ghost"
            onClick={close}
          >
            <XIcon />
          </Button>
        </div>
      </header>

      {nodeIssues.length > 0 ? (
        <div
          aria-label="节点校验问题"
          className="border-border bg-muted/40 space-y-1 border-b px-4 py-3"
        >
          {nodeIssues.map((issue, index) => (
            <button
              aria-label={`定位校验问题：${issue.message}`}
              className={cn(
                "focus-visible:ring-ring block w-full rounded-sm text-left text-xs focus-visible:ring-2 focus-visible:outline-none",
                issue.severity === "error"
                  ? "text-destructive"
                  : "text-muted-foreground",
              )}
              data-slot="workflow-validation-issue-focus"
              key={`${issue.code}:${issue.path.join(".")}:${index}`}
              type="button"
              onClick={() =>
                focusWorkflowInspectorIssue(store, issue, onFocusIssue)
              }
            >
              {issue.message}
            </button>
          ))}
        </div>
      ) : null}

      <Tabs
        className="min-h-0 flex-1 gap-0"
        onValueChange={changeTab}
        value={session.inspector.tab}
      >
        <TabsList
          aria-label="节点检查器视图"
          className="border-border h-11 w-full justify-start rounded-none border-b px-4"
          variant="line"
        >
          <TabsTrigger value="settings">设置</TabsTrigger>
          <TabsTrigger value="last_run">上次运行</TabsTrigger>
        </TabsList>

        <TabsContent className="min-h-0" value="settings">
          <div
            className="h-full"
            data-restored-scroll-top={session.inspector.scroll_top}
            ref={settingsScrollContainerRef}
            onScrollCapture={rememberScroll}
          >
            <ScrollArea className="h-full">
              <WorkflowInspectorSettingsDisabledContext.Provider
                value={settingsDisabled}
              >
                {children ??
                  (catalogEntry && node.nodeType ? (
                    <WorkflowNodeConfig
                      capabilities={capabilities}
                      catalogEntry={catalogEntry}
                      disabled={settingsDisabled}
                      document={snapshot.current}
                      locale={locale}
                      modelCatalog={modelCatalog}
                      node={node.draft}
                      nodeId={node.id}
                      readOnly={readOnly}
                      registry={WORKFLOW_NODE_CONFIG_PANELS}
                    />
                  ) : (
                    <p className="text-muted-foreground p-4 text-sm">
                      该节点没有可用的专用配置，已保持只读。
                    </p>
                  ))}
              </WorkflowInspectorSettingsDisabledContext.Provider>
              <Separator />
              {sourcePorts.map((sourcePort) => (
                <WorkflowNextStep
                  capabilities={capabilities}
                  catalog={catalog}
                  createNextStepCommand={createNextStepCommand}
                  disabled={settingsDisabled || incomplete}
                  key={sourcePort.id}
                  locale={locale}
                  node={node}
                  sourcePort={sourcePort}
                />
              ))}
            </ScrollArea>
          </div>
        </TabsContent>

        <TabsContent className="min-h-0" value="last_run">
          <ScrollArea className="h-full">
            <WorkflowInspectorLastRun lastRun={lastRun} nodeId={node.id} />
          </ScrollArea>
        </TabsContent>
      </Tabs>
    </div>
  );
}

export function WorkflowInspector({
  presentation = "panel",
  ...props
}: WorkflowInspectorProps) {
  const store = useWorkflowWorkbenchStore();
  const snapshot = useWorkflowWorkbenchSnapshot();

  if (presentation === "sheet") {
    return (
      <Sheet
        open={snapshot.editorSession.inspector.open}
        onOpenChange={(open) => {
          if (open) return;
          updateWorkflowEditorSession(store, (session) => ({
            ...session,
            inspector: { ...session.inspector, open: false, node_id: null },
            palette: { open: false, anchor: null },
          }));
        }}
      >
        <SheetContent
          aria-describedby={undefined}
          className="w-full gap-0 p-0 sm:max-w-none"
          closeLabel="关闭节点检查器"
          showCloseButton={false}
        >
          <SheetTitle className="sr-only">节点检查器</SheetTitle>
          <SheetDescription className="sr-only">
            编辑节点设置或查看该节点上次运行的安全投影。
          </SheetDescription>
          <WorkflowInspectorBody {...props} />
        </SheetContent>
      </Sheet>
    );
  }

  return <WorkflowInspectorBody {...props} />;
}
