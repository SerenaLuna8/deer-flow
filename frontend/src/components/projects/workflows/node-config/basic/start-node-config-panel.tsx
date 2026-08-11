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
import { workflowInputDeclSchema } from "@/core/project-workflows/types";

import {
  BasicJsonEditor,
  BasicPanelField,
  BasicPanelShell,
  BasicValueTypeEditor,
  basicPanelWriteDisabled,
  dispatchBasicCommand,
  isRecord,
  nodeConfigOrEmpty,
  recordOrEmpty,
  stableBasicRowId,
  stringOrEmpty,
} from "./shared";

type ReplaceWorkflowInputsCommand = Extract<
  WorkflowWorkbenchCommand,
  { type: "replace_workflow_inputs" }
>;
type WorkflowInputDraft =
  ReplaceWorkflowInputsCommand["workflow_inputs"][number];

const workflowInputs = (
  document: WorkflowPersistedDocumentV1,
): ReplaceWorkflowInputsCommand["workflow_inputs"] =>
  structuredClone(document.spec.workflow_inputs ?? []);

export function buildWorkflowInputReplacement(
  document: WorkflowPersistedDocumentV1,
  index: number,
  patch: Record<string, unknown>,
): ReplaceWorkflowInputsCommand {
  const inputs = workflowInputs(document);
  const current = inputs[index];
  if (!current) throw new RangeError("Workflow input row does not exist");
  inputs[index] = {
    ...current,
    ...structuredClone(patch),
  } as WorkflowInputDraft;
  return { type: "replace_workflow_inputs", workflow_inputs: inputs };
}

export function buildWorkflowInputRemoval(
  document: WorkflowPersistedDocumentV1,
  index: number,
): ReplaceWorkflowInputsCommand {
  const inputs = workflowInputs(document);
  if (!inputs[index]) throw new RangeError("Workflow input row does not exist");
  inputs.splice(index, 1);
  return { type: "replace_workflow_inputs", workflow_inputs: inputs };
}

export function buildWorkflowInputMove(
  document: WorkflowPersistedDocumentV1,
  index: number,
  direction: -1 | 1,
): ReplaceWorkflowInputsCommand {
  const inputs = workflowInputs(document);
  const target = index + direction;
  if (!inputs[index] || target < 0 || target >= inputs.length) {
    throw new RangeError("Workflow input row cannot move there");
  }
  [inputs[index], inputs[target]] = [inputs[target]!, inputs[index]];
  return { type: "replace_workflow_inputs", workflow_inputs: inputs };
}

const newWorkflowInput = (): WorkflowInputDraft => {
  const id = stableBasicRowId("input");
  return {
    id,
    name: id,
    label: null,
    description: null,
    value_type: { kind: "string", collection: false, nullable: false },
    required: false,
    constraints: { kind: "none" },
  };
};

function inputIssues(
  document: WorkflowPersistedDocumentV1,
  nodeConfig: Record<string, unknown>,
): string[] {
  const inputs = document.spec.workflow_inputs ?? [];
  const issues: string[] = [];
  if (Object.keys(nodeConfig).length > 0) {
    issues.push("Start 节点配置必须为空；输入声明只保存在工作流顶层。");
  }
  const ids = new Set<string>();
  const names = new Set<string>();
  inputs.forEach((input, index) => {
    if (!workflowInputDeclSchema.safeParse(input).success) {
      issues.push(`输入 ${index + 1} 的声明尚未完整或类型不合法。`);
    }
    if (typeof input.id === "string") {
      if (ids.has(input.id)) issues.push(`输入 ${index + 1} 的稳定 ID 重复。`);
      ids.add(input.id);
    }
    if (typeof input.name === "string") {
      if (names.has(input.name)) issues.push(`输入变量名 ${input.name} 重复。`);
      names.add(input.name);
    }
  });
  return [...new Set(issues)];
}

const constraintKind = (value: unknown) => {
  const kind = recordOrEmpty(value).kind;
  return ["none", "string", "number", "enum"].includes(stringOrEmpty(kind))
    ? stringOrEmpty(kind)
    : "none";
};

export function StartNodeConfigPanel(props: WorkflowNodeConfigPanelProps) {
  const store = useWorkflowWorkbenchStore();
  const inputs = props.document.spec.workflow_inputs ?? [];
  const locked = basicPanelWriteDisabled(props);
  const dispatch = (command: ReplaceWorkflowInputsCommand) =>
    dispatchBasicCommand(store, command);
  const replace = (index: number, patch: Record<string, unknown>) =>
    dispatch(buildWorkflowInputReplacement(props.document, index, patch));

  return (
    <BasicPanelShell
      disabled={locked && !props.readOnly}
      issues={inputIssues(props.document, nodeConfigOrEmpty(props.node))}
      readOnly={props.readOnly}
      title="工作流输入"
    >
      <p className="text-muted-foreground text-xs">
        输入声明的稳定 ID 与顺序属于工作流语义；重命名不会改变已有绑定。
      </p>
      {inputs.length === 0 ? (
        <p className="text-muted-foreground text-sm">尚未声明工作流输入。</p>
      ) : null}
      {inputs.map((input, index) => {
        const row = isRecord(input) ? input : {};
        const valueType = recordOrEmpty(row.value_type);
        const constraints = recordOrEmpty(row.constraints);
        const kind = constraintKind(constraints);
        return (
          <section
            aria-label={`工作流输入 ${index + 1}`}
            className="border-border space-y-3 rounded-lg border p-3"
            key={stringOrEmpty(row.id) || `input-${index}`}
          >
            <input type="hidden" value={stringOrEmpty(row.id)} />
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sm font-medium">
                {stringOrEmpty(row.name) || `输入 ${index + 1}`}
              </h4>
              <div className="flex gap-1">
                <Button
                  aria-label={`上移输入 ${index + 1}`}
                  disabled={index === 0}
                  onClick={() =>
                    dispatch(buildWorkflowInputMove(props.document, index, -1))
                  }
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  上移
                </Button>
                <Button
                  aria-label={`下移输入 ${index + 1}`}
                  disabled={index === inputs.length - 1}
                  onClick={() =>
                    dispatch(buildWorkflowInputMove(props.document, index, 1))
                  }
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  下移
                </Button>
                <Button
                  onClick={() =>
                    dispatch(buildWorkflowInputRemoval(props.document, index))
                  }
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  删除
                </Button>
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <BasicPanelField label="变量名">
                <Input
                  aria-label={`输入 ${index + 1} 变量名`}
                  onChange={(event) =>
                    replace(index, { name: event.currentTarget.value })
                  }
                  value={stringOrEmpty(row.name)}
                />
              </BasicPanelField>
              <BasicPanelField label="显示标签">
                <Input
                  aria-label={`输入 ${index + 1} 显示标签`}
                  onChange={(event) =>
                    replace(index, {
                      label: event.currentTarget.value || null,
                    })
                  }
                  value={stringOrEmpty(row.label)}
                />
              </BasicPanelField>
            </div>
            <BasicPanelField label="帮助文本">
              <Textarea
                aria-label={`输入 ${index + 1} 帮助文本`}
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
              value={valueType}
            />
            <label className="flex items-center gap-2 text-sm">
              <input
                checked={row.required === true}
                onChange={(event) =>
                  replace(index, { required: event.currentTarget.checked })
                }
                type="checkbox"
              />
              必填
            </label>
            <BasicJsonEditor
              label="默认值"
              onChange={(value) => replace(index, { default: value })}
              value={Object.hasOwn(row, "default") ? row.default : null}
            />
            <div className="space-y-2">
              <BasicPanelField label="约束">
                <select
                  aria-label={`输入 ${index + 1} 约束类型`}
                  className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
                  onChange={(event) => {
                    const nextKind = event.currentTarget.value;
                    const next =
                      nextKind === "string"
                        ? { kind: "string" }
                        : nextKind === "number"
                          ? { kind: "number" }
                          : nextKind === "enum"
                            ? { kind: "enum", options: [] }
                            : { kind: "none" };
                    replace(index, { constraints: next });
                  }}
                  value={kind}
                >
                  <option value="none">无</option>
                  <option value="string">字符串</option>
                  <option value="number">数值</option>
                  <option value="enum">枚举</option>
                </select>
              </BasicPanelField>
              {kind === "string" ? (
                <div className="grid gap-2 sm:grid-cols-3">
                  <Input
                    aria-label="最小长度"
                    min={0}
                    onChange={(event) =>
                      replace(index, {
                        constraints: {
                          ...constraints,
                          kind: "string",
                          min_length: Number(event.currentTarget.value),
                        },
                      })
                    }
                    type="number"
                    value={
                      typeof constraints.min_length === "number"
                        ? constraints.min_length
                        : ""
                    }
                  />
                  <Input
                    aria-label="最大长度"
                    min={0}
                    onChange={(event) =>
                      replace(index, {
                        constraints: {
                          ...constraints,
                          kind: "string",
                          max_length: Number(event.currentTarget.value),
                        },
                      })
                    }
                    type="number"
                    value={
                      typeof constraints.max_length === "number"
                        ? constraints.max_length
                        : ""
                    }
                  />
                  <Input
                    aria-label="正则模式"
                    onChange={(event) =>
                      replace(index, {
                        constraints: {
                          ...constraints,
                          kind: "string",
                          pattern: event.currentTarget.value,
                        },
                      })
                    }
                    value={stringOrEmpty(constraints.pattern)}
                  />
                </div>
              ) : null}
              {kind === "number" ? (
                <div className="grid gap-2 sm:grid-cols-2">
                  <Input
                    aria-label="最小值"
                    onChange={(event) =>
                      replace(index, {
                        constraints: {
                          ...constraints,
                          kind: "number",
                          minimum: Number(event.currentTarget.value),
                        },
                      })
                    }
                    type="number"
                    value={
                      typeof constraints.minimum === "number"
                        ? constraints.minimum
                        : ""
                    }
                  />
                  <Input
                    aria-label="最大值"
                    onChange={(event) =>
                      replace(index, {
                        constraints: {
                          ...constraints,
                          kind: "number",
                          maximum: Number(event.currentTarget.value),
                        },
                      })
                    }
                    type="number"
                    value={
                      typeof constraints.maximum === "number"
                        ? constraints.maximum
                        : ""
                    }
                  />
                </div>
              ) : null}
              {kind === "enum" ? (
                <BasicJsonEditor
                  label="枚举选项"
                  onChange={(value) =>
                    Array.isArray(value) &&
                    replace(index, {
                      constraints: { kind: "enum", options: value },
                    })
                  }
                  value={
                    Array.isArray(constraints.options)
                      ? constraints.options
                      : []
                  }
                />
              ) : null}
            </div>
          </section>
        );
      })}
      <Button
        onClick={() =>
          dispatch({
            type: "replace_workflow_inputs",
            workflow_inputs: [
              ...workflowInputs(props.document),
              newWorkflowInput(),
            ],
          })
        }
        type="button"
        variant="outline"
      >
        添加输入
      </Button>
    </BasicPanelShell>
  );
}
