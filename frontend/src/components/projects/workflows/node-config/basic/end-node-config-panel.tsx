"use client";

import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import {
  useWorkflowWorkbenchStore,
  type WorkflowWorkbenchCommand,
} from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import { workflowOutputDeclSchema } from "@/core/project-workflows/types";

import {
  BasicBindingEditor,
  BasicJsonEditor,
  BasicPanelField,
  BasicPanelShell,
  BasicValueTypeEditor,
  basicPanelWriteDisabled,
  dispatchBasicCommand,
  isRecord,
  nodeConfigOrEmpty,
  recordOrEmpty,
  safeJsonText,
  stableBasicRowId,
  stringOrEmpty,
} from "./shared";

type ReplaceWorkflowOutputsCommand = Extract<
  WorkflowWorkbenchCommand,
  { type: "replace_workflow_outputs" }
>;
type WorkflowOutputDraft =
  ReplaceWorkflowOutputsCommand["workflow_outputs"][number];

const workflowOutputs = (
  document: WorkflowPersistedDocumentV1,
): ReplaceWorkflowOutputsCommand["workflow_outputs"] =>
  structuredClone(document.spec.workflow_outputs ?? []);

export function buildWorkflowOutputReplacement(
  document: WorkflowPersistedDocumentV1,
  index: number,
  patch: Record<string, unknown>,
): ReplaceWorkflowOutputsCommand {
  const outputs = workflowOutputs(document);
  const current = outputs[index];
  if (!current) throw new RangeError("Workflow output row does not exist");
  outputs[index] = {
    ...current,
    ...structuredClone(patch),
  } as WorkflowOutputDraft;
  return { type: "replace_workflow_outputs", workflow_outputs: outputs };
}

export function buildWorkflowOutputRemoval(
  document: WorkflowPersistedDocumentV1,
  index: number,
): ReplaceWorkflowOutputsCommand {
  const outputs = workflowOutputs(document);
  if (!outputs[index])
    throw new RangeError("Workflow output row does not exist");
  outputs.splice(index, 1);
  return { type: "replace_workflow_outputs", workflow_outputs: outputs };
}

export function buildWorkflowOutputMove(
  document: WorkflowPersistedDocumentV1,
  index: number,
  direction: -1 | 1,
): ReplaceWorkflowOutputsCommand {
  const outputs = workflowOutputs(document);
  const target = index + direction;
  if (!outputs[index] || target < 0 || target >= outputs.length) {
    throw new RangeError("Workflow output row cannot move there");
  }
  [outputs[index], outputs[target]] = [outputs[target]!, outputs[index]];
  return { type: "replace_workflow_outputs", workflow_outputs: outputs };
}

const newWorkflowOutput = (): WorkflowOutputDraft => {
  const id = stableBasicRowId("output");
  return {
    id,
    name: id,
    description: null,
    value_type: { kind: "string", collection: false, nullable: false },
    source: null,
  };
};

function outputIssues(
  document: WorkflowPersistedDocumentV1,
  nodeConfig: Record<string, unknown>,
): string[] {
  const outputs = document.spec.workflow_outputs ?? [];
  const issues: string[] = [];
  if (Object.keys(nodeConfig).length > 0) {
    issues.push("End 节点配置必须为空；输出声明只保存在工作流顶层。");
  }
  const ids = new Set<string>();
  const names = new Set<string>();
  outputs.forEach((output, index) => {
    if (!workflowOutputDeclSchema.safeParse(output).success) {
      issues.push(`输出 ${index + 1} 的声明尚未完整或类型不合法。`);
    }
    if (typeof output.id === "string") {
      if (ids.has(output.id)) issues.push(`输出 ${index + 1} 的稳定 ID 重复。`);
      ids.add(output.id);
    }
    if (typeof output.name === "string") {
      if (names.has(output.name))
        issues.push(`输出变量名 ${output.name} 重复。`);
      names.add(output.name);
    }
    if (output.source === null || output.source === undefined) {
      issues.push(`输出 ${index + 1} 尚未绑定来源。`);
    }
  });
  return [...new Set(issues)];
}

export function EndNodeConfigPanel(props: WorkflowNodeConfigPanelProps) {
  const store = useWorkflowWorkbenchStore();
  const outputs = props.document.spec.workflow_outputs ?? [];
  const locked = basicPanelWriteDisabled(props);
  const dispatch = (command: ReplaceWorkflowOutputsCommand) =>
    dispatchBasicCommand(store, command);
  const replace = (index: number, patch: Record<string, unknown>) =>
    dispatch(buildWorkflowOutputReplacement(props.document, index, patch));

  return (
    <BasicPanelShell
      disabled={locked && !props.readOnly}
      issues={outputIssues(props.document, nodeConfigOrEmpty(props.node))}
      readOnly={props.readOnly}
      title="工作流输出"
    >
      <p className="text-muted-foreground text-xs">
        End 只映射顶层输出，不提供 outgoing Handle 或“下一步”。
      </p>
      {outputs.length === 0 ? (
        <p className="text-muted-foreground text-sm">尚未声明工作流输出。</p>
      ) : null}
      {outputs.map((output, index) => {
        const row = isRecord(output) ? output : {};
        return (
          <section
            aria-label={`工作流输出 ${index + 1}`}
            className="border-border space-y-3 rounded-lg border p-3"
            key={stringOrEmpty(row.id) || `output-${index}`}
          >
            <input type="hidden" value={stringOrEmpty(row.id)} />
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sm font-medium">
                {stringOrEmpty(row.name) || `输出 ${index + 1}`}
              </h4>
              <div className="flex gap-1">
                <Button
                  aria-label={`上移输出 ${index + 1}`}
                  disabled={index === 0}
                  onClick={() =>
                    dispatch(buildWorkflowOutputMove(props.document, index, -1))
                  }
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  上移
                </Button>
                <Button
                  aria-label={`下移输出 ${index + 1}`}
                  disabled={index === outputs.length - 1}
                  onClick={() =>
                    dispatch(buildWorkflowOutputMove(props.document, index, 1))
                  }
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  下移
                </Button>
                <Button
                  onClick={() =>
                    dispatch(buildWorkflowOutputRemoval(props.document, index))
                  }
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  删除
                </Button>
              </div>
            </div>
            <BasicPanelField label="变量名">
              <Input
                aria-label={`输出 ${index + 1} 变量名`}
                onChange={(event) =>
                  replace(index, { name: event.currentTarget.value })
                }
                value={stringOrEmpty(row.name)}
              />
            </BasicPanelField>
            <BasicPanelField label="说明">
              <Textarea
                aria-label={`输出 ${index + 1} 说明`}
                onChange={(event) =>
                  replace(index, {
                    description: event.currentTarget.value || null,
                  })
                }
                value={stringOrEmpty(row.description)}
              />
            </BasicPanelField>
            <BasicValueTypeEditor
              onChange={(next) => replace(index, { value_type: next })}
              value={recordOrEmpty(row.value_type)}
            />
            <BasicBindingEditor
              label="输出来源"
              onChange={(source) => replace(index, { source })}
              value={row.source}
            />
            <BasicJsonEditor
              label="缺省值"
              onChange={(value) => replace(index, { default: value })}
              value={Object.hasOwn(row, "default") ? row.default : null}
            />
          </section>
        );
      })}
      <Button
        onClick={() =>
          dispatch({
            type: "replace_workflow_outputs",
            workflow_outputs: [
              ...workflowOutputs(props.document),
              newWorkflowOutput(),
            ],
          })
        }
        type="button"
        variant="outline"
      >
        添加输出
      </Button>
      <section aria-label="最终输出 Schema 预览" className="space-y-2">
        <h4 className="text-sm font-medium">最终输出 Schema（只读预览）</h4>
        <pre className="bg-muted overflow-auto rounded-md p-3 text-xs">
          {safeJsonText(
            outputs.map((output) => ({
              id: output.id ?? null,
              name: output.name ?? null,
              value_type: output.value_type ?? null,
            })),
            "[]",
          )}
        </pre>
      </section>
    </BasicPanelShell>
  );
}
