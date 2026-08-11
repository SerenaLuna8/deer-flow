"use client";

import { useState } from "react";

import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import {
  useWorkflowWorkbenchStore,
  type WorkflowWorkbenchCommand,
} from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  loopNodeConfigV1Schema,
  predicateAstSchema,
  workflowValueTypeSchema,
  type LoopNodeConfigV1,
  type PredicateAst,
  type ValueBinding,
} from "@/core/project-workflows/types";

import {
  BranchLoopPanelShell,
  PredicateAstEditor,
  TypedValueBindingEditor,
  WorkflowValueTypeEditor,
  arrayOrEmpty,
  bindingOptionsForDocument,
  panelLocked,
  recordOrEmpty,
  safePredicateAst,
  safeValueBinding,
  safeValueType,
  stableNodeId,
  stableSemanticId,
  stringOrEmpty,
  type TypedBindingOptions,
} from "../branching/shared";

export const LOOP_NATIVE_MAX_ITERATIONS = 1_000_000;
const LOOP_NATIVE_MAX_VARIABLES = 252;

type LoopVariable = LoopNodeConfigV1["variables"][number];
type DraftNode = WorkflowNodeConfigPanelProps["node"];
type AddLoopBodyEntryCommand = Extract<
  WorkflowWorkbenchCommand,
  { type: "add_loop_body_entry" }
>;
type SetLoopBodyExitCommand = Extract<
  WorkflowWorkbenchCommand,
  { type: "set_loop_body_exit" }
>;
type ReparentNodeCommand = Extract<
  WorkflowWorkbenchCommand,
  { type: "reparent_node" }
>;
type UpdateNodeBindingsCommand = Extract<
  WorkflowWorkbenchCommand,
  { type: "update_node_input_bindings" }
>;

export type LoopVariableIdentity = {
  id: string;
  name: string;
  initialInputId: string;
  nextInputId: string;
  outputPortId: string;
};

export type LoopBodyNodeType =
  | "llm"
  | "condition"
  | "transform"
  | "variable_aggregate"
  | "http_request"
  | "python_code";

export type AddLoopBodyEntryInput = {
  loopNodeId: string;
  nodeId: string;
  nodeType: LoopBodyNodeType;
  position: { x: number; y: number };
  setAsExit?: boolean;
};

const defaultTermination = (): PredicateAst => ({
  op: "and",
  items: [
    {
      left: { kind: "literal", value: true },
      operator: "eq",
      right: { kind: "literal", value: true },
    },
  ],
});

export function appendLoopVariable(
  config: LoopNodeConfigV1,
  identity: LoopVariableIdentity,
  maximum = LOOP_NATIVE_MAX_VARIABLES,
): LoopNodeConfigV1 {
  const limit = Math.min(LOOP_NATIVE_MAX_VARIABLES, Math.max(1, maximum));
  const occupiedPorts = new Set([
    "body",
    "next",
    "error",
    "iteration_count",
    ...config.variables.map((variable) => variable.output_port_id),
  ]);
  if (
    config.variables.length >= limit ||
    !identity.id ||
    !identity.name ||
    !identity.initialInputId ||
    !identity.nextInputId ||
    !identity.outputPortId ||
    occupiedPorts.has(identity.outputPortId) ||
    config.variables.some(
      (variable) =>
        variable.id === identity.id ||
        variable.initial_input_id === identity.initialInputId ||
        variable.next_input_id === identity.nextInputId,
    )
  ) {
    return config;
  }
  return {
    ...config,
    variables: [
      ...config.variables,
      {
        id: identity.id,
        name: identity.name,
        value_type: safeValueType(undefined),
        initial_input_id: identity.initialInputId,
        next_input_id: identity.nextInputId,
        output_port_id: identity.outputPortId,
      },
    ],
  };
}

export function moveLoopVariable(
  config: LoopNodeConfigV1,
  fromIndex: number,
  toIndex: number,
): LoopNodeConfigV1 {
  if (
    fromIndex === toIndex ||
    fromIndex < 0 ||
    fromIndex >= config.variables.length ||
    toIndex < 0 ||
    toIndex >= config.variables.length
  ) {
    return config;
  }
  const variables = [...config.variables];
  const [moved] = variables.splice(fromIndex, 1);
  if (!moved) return config;
  variables.splice(toIndex, 0, moved);
  return { ...config, variables };
}

export function removeLoopVariable(
  config: LoopNodeConfigV1,
  index: number,
): LoopNodeConfigV1 {
  if (
    config.variables.length <= 1 ||
    index < 0 ||
    index >= config.variables.length
  ) {
    return config;
  }
  return {
    ...config,
    variables: config.variables.filter(
      (_, variableIndex) => variableIndex !== index,
    ),
  };
}

export function buildLoopBindingUpdate(
  node: DraftNode,
  inputId: string,
  binding: ValueBinding | null,
): UpdateNodeBindingsCommand {
  const next: Record<string, ValueBinding | null> = {};
  for (const [id, candidate] of Object.entries(
    recordOrEmpty(node.input_bindings),
  )) {
    if (candidate === null) {
      next[id] = null;
      continue;
    }
    const parsed = safeValueBinding(candidate);
    if (parsed) next[id] = parsed;
  }
  next[inputId] = binding;
  return {
    type: "update_node_input_bindings",
    node_id: stringOrEmpty(node.id),
    input_bindings: next,
  };
}

export function buildAddLoopBodyEntryCommand({
  loopNodeId,
  nodeId,
  nodeType,
  position,
  setAsExit = true,
}: AddLoopBodyEntryInput): AddLoopBodyEntryCommand {
  const supported: readonly LoopBodyNodeType[] = [
    "llm",
    "condition",
    "transform",
    "variable_aggregate",
    "http_request",
    "python_code",
  ];
  if (!supported.includes(nodeType)) {
    throw new Error("Nested Loop and terminal nodes cannot enter a Loop body");
  }
  return {
    type: "add_loop_body_entry",
    loop_node_id: loopNodeId,
    node: {
      id: nodeId,
      type: nodeType,
      type_version: 1,
      scope: { kind: "root" },
      custom_label: null,
      description: null,
      input_bindings: {},
      execution_policy: {
        retry: { mode: "none" },
        on_error: { mode: "fail_workflow" },
      },
      config: {},
    },
    layout: { node_id: nodeId, position },
    set_as_exit: setAsExit,
  };
}

export const buildLoopBodyEntryCommand = buildAddLoopBodyEntryCommand;

export function buildSetLoopBodyExitCommand(
  loopNodeId: string,
  nodeId: string | null,
): SetLoopBodyExitCommand {
  return {
    type: "set_loop_body_exit",
    loop_node_id: loopNodeId,
    node_id: nodeId,
  };
}

export const buildLoopBodyExitCommand = buildSetLoopBodyExitCommand;

export function buildReparentLoopChildCommand(
  nodeId: string,
  parentNodeId: string | null,
): ReparentNodeCommand {
  return {
    type: "reparent_node",
    node_id: nodeId,
    parent_node_id: parentNodeId,
  };
}

export const buildLoopReparentCommand = buildReparentLoopChildCommand;

const readLoopConfig = (value: unknown): LoopNodeConfigV1 => {
  const exact = loopNodeConfigV1Schema.safeParse(value);
  if (exact.success) return exact.data;
  const config = recordOrEmpty(value);
  const variables = arrayOrEmpty(config.variables).flatMap((candidate) => {
    const variable = recordOrEmpty(candidate);
    const valueType = workflowValueTypeSchema.safeParse(variable.value_type);
    const id = stringOrEmpty(variable.id);
    const name = stringOrEmpty(variable.name);
    const initialInputId = stringOrEmpty(variable.initial_input_id);
    const nextInputId = stringOrEmpty(variable.next_input_id);
    const outputPortId = stringOrEmpty(variable.output_port_id);
    if (
      !valueType.success ||
      !id ||
      !name ||
      !initialInputId ||
      !nextInputId ||
      !outputPortId
    ) {
      return [];
    }
    return [
      {
        id,
        name,
        value_type: valueType.data,
        initial_input_id: initialInputId,
        next_input_id: nextInputId,
        output_port_id: outputPortId,
      },
    ];
  });
  const termination = safePredicateAst(config.termination_condition);
  const maxIterations = config.max_iterations;
  return {
    mode: "do_until",
    body_entry_node_id: stringOrEmpty(config.body_entry_node_id),
    body_exit_node_id: stringOrEmpty(config.body_exit_node_id),
    max_iterations:
      typeof maxIterations === "number" && Number.isSafeInteger(maxIterations)
        ? maxIterations
        : 1,
    termination_condition: termination ?? defaultTermination(),
    variables,
  };
};

const cleanLoopDraftConfig = (
  config: LoopNodeConfigV1,
): Record<string, unknown> => ({
  mode: "do_until",
  ...(config.body_entry_node_id
    ? { body_entry_node_id: config.body_entry_node_id }
    : {}),
  ...(config.body_exit_node_id
    ? { body_exit_node_id: config.body_exit_node_id }
    : {}),
  max_iterations: config.max_iterations,
  termination_condition: config.termination_condition,
  ...(config.variables.length > 0 ? { variables: config.variables } : {}),
});

const loopBodyChildren = (props: WorkflowNodeConfigPanelProps) =>
  (props.document.spec.nodes ?? []).filter((node) => {
    const scope = recordOrEmpty(node.scope);
    const layout = (props.document.canvas.node_layouts ?? []).find(
      (candidate) => candidate.node_id === node.id,
    );
    return (
      scope.kind === "loop_body" &&
      scope.loop_node_id === props.nodeId &&
      layout?.parent_node_id === props.nodeId
    );
  });

const eligibleRootNodes = (props: WorkflowNodeConfigPanelProps) =>
  (props.document.spec.nodes ?? []).filter((node) => {
    const type = stringOrEmpty(node.type);
    return (
      node.id !== props.nodeId &&
      recordOrEmpty(node.scope).kind === "root" &&
      !["start", "end", "loop"].includes(type)
    );
  });

const loopVariableReferenced = (
  props: WorkflowNodeConfigPanelProps,
  variable: LoopVariable,
): boolean => {
  const matches = (value: unknown): boolean => {
    if (Array.isArray(value)) return value.some(matches);
    if (!value || typeof value !== "object") return false;
    const record = value as Record<string, unknown>;
    if (
      (record.kind === "loop_variable" &&
        record.loop_node_id === props.nodeId &&
        record.variable_id === variable.id) ||
      (record.kind === "node_output" &&
        record.node_id === props.nodeId &&
        record.output_id === variable.output_port_id)
    ) {
      return true;
    }
    return Object.values(record).some(matches);
  };
  return matches({
    nodes: props.document.spec.nodes,
    outputs: props.document.spec.workflow_outputs,
  });
};

const ownLoopBindingOptions = (
  nodeId: string,
  variables: readonly LoopVariable[],
): TypedBindingOptions => ({
  workflowInputs: [],
  nodeOutputs: [
    {
      nodeId,
      outputId: "iteration_count",
      label: "iteration_count · committed counter",
    },
  ],
  loopVariables: variables.map((variable) => ({
    loopNodeId: nodeId,
    variableId: variable.id,
    label: `${variable.name} · committed loop variable`,
  })),
});

const predicateOnlyReadsOwnLoopState = (
  value: PredicateAst,
  nodeId: string,
  variableIds: ReadonlySet<string>,
): boolean => {
  const stack: unknown[] = [value];
  while (stack.length > 0) {
    const current = stack.pop();
    if (Array.isArray(current)) {
      stack.push(...current);
      continue;
    }
    if (!current || typeof current !== "object") continue;
    const record = current as Record<string, unknown>;
    if (record.kind === "literal") continue;
    if (typeof record.kind === "string") {
      const ownLoopVariable =
        record.kind === "loop_variable" &&
        record.loop_node_id === nodeId &&
        variableIds.has(stringOrEmpty(record.variable_id));
      const iterationCount =
        record.kind === "node_output" &&
        record.node_id === nodeId &&
        record.output_id === "iteration_count";
      if (!ownLoopVariable && !iterationCount) return false;
    }
    stack.push(...Object.values(record));
  }
  return true;
};

const loopIssues = (
  props: WorkflowNodeConfigPanelProps,
  config: LoopNodeConfigV1,
  maxIterations: number,
): string[] => {
  const issues: string[] = [];
  const raw = recordOrEmpty(props.node.config);
  const bindings = recordOrEmpty(props.node.input_bindings);
  if (config.variables.length === 0) {
    issues.push("至少需要一个循环变量。");
  }
  if (!stringOrEmpty(raw.body_entry_node_id)) {
    issues.push("Loop body entry 尚未通过 compound command 设置。");
  }
  if (!stringOrEmpty(raw.body_exit_node_id)) {
    issues.push("Loop body exit 尚未通过 compound command 设置。");
  }
  if (
    typeof raw.max_iterations !== "number" ||
    !Number.isSafeInteger(raw.max_iterations) ||
    raw.max_iterations < 1 ||
    raw.max_iterations > maxIterations
  ) {
    issues.push(`max_iterations 必须是 1–${maxIterations} 的安全整数。`);
  }
  if (!predicateAstSchema.safeParse(raw.termination_condition).success) {
    issues.push("终止条件 typed Predicate AST 尚未配置。");
  } else if (
    !predicateOnlyReadsOwnLoopState(
      config.termination_condition,
      props.nodeId,
      new Set(config.variables.map((variable) => variable.id)),
    )
  ) {
    issues.push("终止条件只能读取本 Loop 已 commit 变量与 iteration_count。");
  }
  for (const variable of config.variables) {
    if (!safeValueBinding(bindings[variable.initial_input_id])) {
      issues.push(`循环变量 ${variable.name} 的 initial binding 尚未绑定。`);
    }
    if (!safeValueBinding(bindings[variable.next_input_id])) {
      issues.push(`循环变量 ${variable.name} 的 next binding 尚未绑定。`);
    }
  }
  if (
    loopBodyChildren(props).some((node) => stringOrEmpty(node.type) === "loop")
  ) {
    issues.push("首批不允许 nested Loop。");
  }
  if (!loopNodeConfigV1Schema.safeParse(props.node.config).success) {
    issues.push("Loop Draft 尚未满足 strict config；可逐项补齐后再发布。");
  }
  return [...new Set(issues)];
};

function LoopVariableEditor({
  bindingOptions,
  config,
  index,
  locked,
  onBindingChange,
  onCommit,
  props,
  variable,
}: {
  bindingOptions: TypedBindingOptions;
  config: LoopNodeConfigV1;
  index: number;
  locked: boolean;
  onBindingChange: (inputId: string, value: ValueBinding) => void;
  onCommit: (next: LoopNodeConfigV1) => void;
  props: WorkflowNodeConfigPanelProps;
  variable: LoopVariable;
}) {
  const bindings = recordOrEmpty(props.node.input_bindings);
  const referenced = loopVariableReferenced(props, variable);
  const updateVariable = (patch: Partial<LoopVariable>) => {
    const variables = [...config.variables];
    variables[index] = { ...variable, ...patch };
    onCommit({ ...config, variables });
  };
  return (
    <article
      aria-label={`循环变量 ${index + 1}`}
      className="border-border space-y-3 rounded-md border p-3"
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium">循环变量 {index + 1}</p>
          <p className="text-muted-foreground text-[11px]">
            stable output: <code>{variable.output_port_id}</code>
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          <Button
            aria-label={`上移循环变量 ${index + 1}`}
            disabled={locked || index === 0}
            onClick={() => onCommit(moveLoopVariable(config, index, index - 1))}
            size="sm"
            type="button"
            variant="ghost"
          >
            上移
          </Button>
          <Button
            aria-label={`下移循环变量 ${index + 1}`}
            disabled={locked || index === config.variables.length - 1}
            onClick={() => onCommit(moveLoopVariable(config, index, index + 1))}
            size="sm"
            type="button"
            variant="ghost"
          >
            下移
          </Button>
          <Button
            aria-label={`删除循环变量 ${index + 1}`}
            disabled={locked || config.variables.length <= 1 || referenced}
            onClick={() => onCommit(removeLoopVariable(config, index))}
            size="sm"
            type="button"
            variant="ghost"
          >
            删除
          </Button>
        </div>
      </header>
      <label className="block space-y-1 text-xs">
        <span>变量名</span>
        <Input
          aria-label={`循环变量 ${index + 1} 名称`}
          disabled={locked}
          maxLength={128}
          onChange={(event) => {
            if (event.currentTarget.value) {
              updateVariable({ name: event.currentTarget.value });
            }
          }}
          value={variable.name}
        />
      </label>
      <WorkflowValueTypeEditor
        disabled={locked}
        label={`循环变量 ${index + 1} value type`}
        onChange={(valueType) => updateVariable({ value_type: valueType })}
        value={variable.value_type}
      />
      <p className="text-muted-foreground text-[11px]">
        initial key: <code>{variable.initial_input_id}</code> · next key:{" "}
        <code>{variable.next_input_id}</code>
      </p>
      <TypedValueBindingEditor
        disabled={locked}
        label={`${variable.name} initial typed binding`}
        onChange={(value) => onBindingChange(variable.initial_input_id, value)}
        options={bindingOptions}
        value={bindings[variable.initial_input_id]}
      />
      <TypedValueBindingEditor
        disabled={locked}
        label={`${variable.name} next typed binding`}
        onChange={(value) => onBindingChange(variable.next_input_id, value)}
        options={bindingOptions}
        value={bindings[variable.next_input_id]}
      />
      {referenced ? (
        <p className="text-muted-foreground text-xs" role="status">
          该变量或 stable output 已被引用，需先清理引用才能删除。
        </p>
      ) : null}
    </article>
  );
}

export function LoopNodeConfigPanel(props: WorkflowNodeConfigPanelProps) {
  const store = useWorkflowWorkbenchStore();
  const locked = panelLocked(props);
  const config = readLoopConfig(props.node.config);
  const maxIterations = Math.min(
    LOOP_NATIVE_MAX_ITERATIONS,
    props.catalogEntry.public_limits?.max_iterations ??
      LOOP_NATIVE_MAX_ITERATIONS,
  );
  const issues = loopIssues(props, config, maxIterations);
  const bindingOptions = bindingOptionsForDocument(
    props.document,
    props.locale,
  );
  const children = loopBodyChildren(props);
  const rootCandidates = eligibleRootNodes(props);
  const [bodyNodeType, setBodyNodeType] =
    useState<LoopBodyNodeType>("transform");
  const [rootNodeToMove, setRootNodeToMove] = useState("");

  const commit = (next: LoopNodeConfigV1) => {
    if (locked || next === config) return;
    store.dispatch({
      type: "update_node_config",
      node_id: props.nodeId,
      config: cleanLoopDraftConfig(next),
    });
  };

  const updateBinding = (inputId: string, value: ValueBinding) => {
    if (locked) return;
    store.dispatch(buildLoopBindingUpdate(props.node, inputId, value));
  };

  return (
    <BranchLoopPanelShell
      disabled={props.disabled}
      issues={issues}
      readOnly={props.readOnly}
      title="有界循环"
    >
      <section
        aria-label="do until 语义"
        className="border-border bg-muted/30 space-y-1 rounded-md border p-3"
      >
        <p className="text-sm font-medium">do_until（固定）</p>
        <p className="text-muted-foreground text-xs">
          body 至少执行一次；全部循环变量原子更新并递增 iteration
          后，再求值终止条件。
        </p>
        <p className="text-muted-foreground text-xs">
          达到上限仍不满足时稳定失败为
          WORKFLOW_LOOP_LIMIT_EXCEEDED，不静默截断。
        </p>
      </section>

      <section className="space-y-3" aria-label="循环变量">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h4 className="text-sm font-medium">循环变量</h4>
            <p className="text-muted-foreground text-xs">
              至少 1 项；ID、initial/next binding key 与 output port
              均保持稳定。
            </p>
          </div>
          <Button
            disabled={
              locked || config.variables.length >= LOOP_NATIVE_MAX_VARIABLES
            }
            onClick={() => {
              const token = stableSemanticId("loop_var");
              commit(
                appendLoopVariable(config, {
                  id: token,
                  name: `variable_${config.variables.length + 1}`,
                  initialInputId: `${token}_initial`,
                  nextInputId: `${token}_next`,
                  outputPortId: `${token}_value`,
                }),
              );
            }}
            size="sm"
            type="button"
            variant="outline"
          >
            添加循环变量
          </Button>
        </div>
        {config.variables.map((variable, index) => (
          <LoopVariableEditor
            bindingOptions={bindingOptions}
            config={config}
            index={index}
            key={variable.id}
            locked={locked}
            onBindingChange={updateBinding}
            onCommit={commit}
            props={props}
            variable={variable}
          />
        ))}
      </section>

      <section className="space-y-3" aria-label="终止条件">
        <div>
          <h4 className="text-sm font-medium">终止条件</h4>
          <p className="text-muted-foreground text-xs">
            每轮变量原子更新后求值；只可读取本 Loop 已 commit 变量与
            iteration_count。
          </p>
        </div>
        <PredicateAstEditor
          allowedBindingKinds={["literal", "loop_variable", "node_output"]}
          disabled={locked}
          label="Loop termination typed Predicate AST"
          onChange={(terminationCondition) =>
            commit({
              ...config,
              termination_condition: terminationCondition,
            })
          }
          options={ownLoopBindingOptions(props.nodeId, config.variables)}
          value={config.termination_condition}
        />
      </section>

      <section className="space-y-2" aria-label="最大迭代次数">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h4 className="text-sm font-medium">max_iterations</h4>
            <p className="text-muted-foreground text-xs">
              Catalog/System 上限：{maxIterations}
            </p>
          </div>
          <Input
            aria-label="最大迭代次数数字输入"
            className="w-28"
            disabled={locked}
            max={maxIterations}
            min={1}
            onChange={(event) => {
              const value = Number(event.currentTarget.value);
              if (
                Number.isSafeInteger(value) &&
                value >= 1 &&
                value <= maxIterations
              ) {
                commit({ ...config, max_iterations: value });
              }
            }}
            step={1}
            type="number"
            value={config.max_iterations}
          />
        </div>
        <input
          aria-label="最大迭代次数滑杆"
          className="w-full"
          disabled={locked}
          max={maxIterations}
          min={1}
          onChange={(event) => {
            const value = Number(event.currentTarget.value);
            if (Number.isSafeInteger(value)) {
              commit({ ...config, max_iterations: value });
            }
          }}
          step={1}
          type="range"
          value={Math.min(maxIterations, Math.max(1, config.max_iterations))}
        />
      </section>

      <section className="space-y-3" aria-label="Loop body scope">
        <div>
          <h4 className="text-sm font-medium">Loop body scope</h4>
          <p className="text-muted-foreground text-xs">
            scope 只由 compound/reparent command 修改，不从 Canvas
            几何包含反推；不创建 authored body edge。
          </p>
        </div>

        <dl className="grid gap-2 text-xs sm:grid-cols-2">
          <div className="border-border rounded-md border p-2">
            <dt className="text-muted-foreground">body entry</dt>
            <dd className="truncate font-mono">
              {config.body_entry_node_id || "未设置"}
            </dd>
          </div>
          <div className="border-border rounded-md border p-2">
            <dt className="text-muted-foreground">body exit</dt>
            <dd className="truncate font-mono">
              {config.body_exit_node_id || "未设置"}
            </dd>
          </div>
        </dl>

        {!config.body_entry_node_id ? (
          <div className="border-border flex flex-wrap gap-2 rounded-md border p-3">
            <select
              aria-label="首个循环体节点类型"
              className="border-input h-9 min-w-0 flex-1 rounded-md border bg-transparent px-2 text-sm"
              disabled={locked}
              onChange={(event) =>
                setBodyNodeType(event.currentTarget.value as LoopBodyNodeType)
              }
              value={bodyNodeType}
            >
              <option value="transform">模板转换</option>
              <option value="llm">大模型</option>
              <option value="condition">条件分支</option>
              <option value="variable_aggregate">变量聚合</option>
              <option value="http_request">HTTP 请求</option>
              <option value="python_code">代码执行</option>
            </select>
            <Button
              disabled={locked}
              onClick={() => {
                if (locked) return;
                store.dispatch(
                  buildAddLoopBodyEntryCommand({
                    loopNodeId: props.nodeId,
                    nodeId: stableNodeId(),
                    nodeType: bodyNodeType,
                    position: { x: 24, y: 80 },
                    setAsExit: true,
                  }),
                );
              }}
              size="sm"
              type="button"
              variant="outline"
            >
              创建 body entry（compound）
            </Button>
          </div>
        ) : null}

        <label className="block space-y-1 text-xs">
          <span>单一 body exit</span>
          <select
            aria-label="Loop body exit"
            className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
            disabled={locked || children.length === 0}
            onChange={(event) => {
              if (locked) return;
              store.dispatch(
                buildSetLoopBodyExitCommand(
                  props.nodeId,
                  event.currentTarget.value || null,
                ),
              );
            }}
            value={config.body_exit_node_id}
          >
            <option value="">未设置</option>
            {children.map((child) => (
              <option
                key={stringOrEmpty(child.id)}
                value={stringOrEmpty(child.id)}
              >
                {stringOrEmpty(child.custom_label) ||
                  stringOrEmpty(child.type) ||
                  stringOrEmpty(child.id)}
              </option>
            ))}
          </select>
        </label>

        <div className="space-y-2">
          <p className="text-xs font-medium">循环体子节点（语义 scope）</p>
          {children.length === 0 ? (
            <p className="text-muted-foreground text-xs">暂无子节点。</p>
          ) : (
            children.map((child) => (
              <div
                className="bg-muted/40 flex items-center justify-between gap-2 rounded-md p-2"
                key={stringOrEmpty(child.id)}
              >
                <span className="min-w-0 truncate text-xs">
                  {stringOrEmpty(child.custom_label) ||
                    stringOrEmpty(child.type) ||
                    stringOrEmpty(child.id)}
                </span>
                <Button
                  aria-label={`将 ${stringOrEmpty(child.id)} 移出循环体`}
                  disabled={locked}
                  onClick={() => {
                    if (locked) return;
                    store.dispatch(
                      buildReparentLoopChildCommand(
                        stringOrEmpty(child.id),
                        null,
                      ),
                    );
                  }}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  移出
                </Button>
              </div>
            ))
          )}
        </div>

        <div className="flex gap-2">
          <select
            aria-label="待移入循环体的 root 节点"
            className="border-input h-9 min-w-0 flex-1 rounded-md border bg-transparent px-2 text-sm"
            disabled={locked || rootCandidates.length === 0}
            onChange={(event) => setRootNodeToMove(event.currentTarget.value)}
            value={rootNodeToMove}
          >
            <option value="">选择 root 节点</option>
            {rootCandidates.map((candidate) => (
              <option
                key={stringOrEmpty(candidate.id)}
                value={stringOrEmpty(candidate.id)}
              >
                {stringOrEmpty(candidate.custom_label) ||
                  stringOrEmpty(candidate.type) ||
                  stringOrEmpty(candidate.id)}
              </option>
            ))}
          </select>
          <Button
            disabled={locked || !rootNodeToMove}
            onClick={() => {
              if (locked || !rootNodeToMove) return;
              store.dispatch(
                buildReparentLoopChildCommand(rootNodeToMove, props.nodeId),
              );
              setRootNodeToMove("");
            }}
            size="sm"
            type="button"
            variant="outline"
          >
            移入（reparent）
          </Button>
        </div>
      </section>
    </BranchLoopPanelShell>
  );
}
