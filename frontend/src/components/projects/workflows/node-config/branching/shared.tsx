"use client";

import { useId, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import { resolveDraftNodePorts } from "@/core/project-workflows/editor/ports";
import {
  predicateAstSchema,
  valueBindingSchema,
  workflowValueTypeSchema,
  type JsonValue,
  type PredicateAst,
  type PredicateClause,
  type ValueBinding,
  type WorkflowValueType,
} from "@/core/project-workflows/types";

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

export const recordOrEmpty = (value: unknown): Record<string, unknown> =>
  isRecord(value) ? value : {};

export const arrayOrEmpty = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];

export const stringOrEmpty = (value: unknown): string =>
  typeof value === "string" ? value : "";

export const panelLocked = ({
  disabled,
  readOnly,
}: {
  disabled: boolean;
  readOnly: boolean;
}): boolean => disabled || readOnly;

export const stableSemanticId = (prefix: string): string => {
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "");
  const fallback = `${Date.now().toString(36)}${Math.random()
    .toString(36)
    .slice(2)}`;
  return `${prefix}_${(random ?? fallback).slice(0, 20)}`;
};

export const stableNodeId = (): string => {
  const generated = globalThis.crypto?.randomUUID?.();
  if (generated) return generated.toLowerCase();
  const digits = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx";
  return digits.replace(/[xy]/gu, (marker) => {
    const random = Math.floor(Math.random() * 16);
    const value = marker === "x" ? random : (random & 0x3) | 0x8;
    return value.toString(16);
  });
};

export function BranchLoopInlineIssues({
  issues,
}: {
  issues: readonly string[];
}) {
  if (issues.length === 0) return null;
  return (
    <ul
      aria-label="配置问题"
      className="border-destructive/40 bg-destructive/5 text-destructive space-y-1 rounded-md border p-3 text-xs"
      role="alert"
    >
      {issues.map((issue) => (
        <li key={issue}>{issue}</li>
      ))}
    </ul>
  );
}

export function BranchLoopPanelShell({
  children,
  disabled,
  issues,
  readOnly,
  title,
}: {
  children: ReactNode;
  disabled: boolean;
  issues: readonly string[];
  readOnly: boolean;
  title: string;
}) {
  const locked = panelLocked({ disabled, readOnly });
  return (
    <section aria-label={title} className="space-y-4 p-4">
      <header className="space-y-1">
        <h3 className="text-sm font-semibold">{title}</h3>
        {locked ? (
          <p className="text-muted-foreground text-xs" role="status">
            {disabled ? "节点当前不可用，配置保持只读。" : "当前为只读模式。"}
          </p>
        ) : null}
      </header>
      <BranchLoopInlineIssues issues={issues} />
      <fieldset
        aria-disabled={locked}
        className="m-0 space-y-4 border-0 p-0"
        disabled={locked}
      >
        <legend className="sr-only">{title}</legend>
        {children}
      </fieldset>
    </section>
  );
}

export function BranchLoopField({
  children,
  help,
  label,
}: {
  children: ReactNode;
  help?: ReactNode;
  label: string;
}) {
  return (
    <label className="block space-y-1.5 text-sm">
      <span className="font-medium">{label}</span>
      {children}
      {help ? (
        <span className="text-muted-foreground block text-xs">{help}</span>
      ) : null}
    </label>
  );
}

export const DEFAULT_VALUE_TYPE: WorkflowValueType = {
  kind: "string",
  collection: false,
  nullable: false,
};

export function safeValueType(value: unknown): WorkflowValueType {
  const parsed = workflowValueTypeSchema.safeParse(value);
  return parsed.success ? parsed.data : DEFAULT_VALUE_TYPE;
}

export function WorkflowValueTypeEditor({
  disabled = false,
  label,
  onChange,
  value,
}: {
  disabled?: boolean;
  label: string;
  onChange: (value: WorkflowValueType) => void;
  value: unknown;
}) {
  const parsed = safeValueType(value);
  return (
    <fieldset
      aria-label={label}
      className="border-border grid gap-2 rounded-md border p-3 sm:grid-cols-3"
      disabled={disabled}
    >
      <legend className="px-1 text-xs font-medium">{label}</legend>
      <label className="space-y-1 text-xs">
        <span>类型</span>
        <select
          aria-label={`${label}类型`}
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          disabled={disabled}
          onChange={(event) =>
            onChange({
              ...parsed,
              kind: event.currentTarget.value as WorkflowValueType["kind"],
            })
          }
          value={parsed.kind}
        >
          <option value="string">string</option>
          <option value="number">number</option>
          <option value="boolean">boolean</option>
          <option value="json">json</option>
          <option value="messages">messages</option>
        </select>
      </label>
      <label className="flex items-center gap-2 self-end pb-2 text-xs">
        <input
          checked={parsed.collection}
          disabled={disabled}
          onChange={(event) =>
            onChange({ ...parsed, collection: event.currentTarget.checked })
          }
          type="checkbox"
        />
        数组
      </label>
      <label className="flex items-center gap-2 self-end pb-2 text-xs">
        <input
          checked={parsed.nullable}
          disabled={disabled}
          onChange={(event) =>
            onChange({ ...parsed, nullable: event.currentTarget.checked })
          }
          type="checkbox"
        />
        可为 null
      </label>
    </fieldset>
  );
}

export type TypedBindingOptions = {
  workflowInputs: Array<{ id: string; label: string }>;
  nodeOutputs: Array<{
    nodeId: string;
    outputId: string;
    label: string;
  }>;
  loopVariables: Array<{
    loopNodeId: string;
    variableId: string;
    label: string;
  }>;
};

export const EMPTY_BINDING_OPTIONS: TypedBindingOptions = {
  workflowInputs: [],
  nodeOutputs: [],
  loopVariables: [],
};

export function bindingOptionsForDocument(
  document: WorkflowPersistedDocumentV1,
  locale: "zh-CN" | "en-US",
): TypedBindingOptions {
  const workflowInputs = (document.spec.workflow_inputs ?? []).flatMap(
    (input) => {
      const id = stringOrEmpty(input.id);
      if (!id) return [];
      return [
        {
          id,
          label: stringOrEmpty(input.label) || stringOrEmpty(input.name) || id,
        },
      ];
    },
  );
  const nodeOutputs = (document.spec.nodes ?? []).flatMap((node) => {
    const nodeId = stringOrEmpty(node.id);
    if (!nodeId) return [];
    return resolveDraftNodePorts(document, nodeId, locale)
      .outputPorts.filter((port) => port.kind === "data")
      .map((port) => ({
        nodeId,
        outputId: port.id,
        label: `${stringOrEmpty(node.custom_label) || stringOrEmpty(node.type) || nodeId} · ${port.label}`,
      }));
  });
  const loopVariables = (document.spec.nodes ?? []).flatMap((node) => {
    if (node.type !== "loop") return [];
    const loopNodeId = stringOrEmpty(node.id);
    if (!loopNodeId) return [];
    return arrayOrEmpty(recordOrEmpty(node.config).variables).flatMap(
      (candidate) => {
        const variable = recordOrEmpty(candidate);
        const variableId = stringOrEmpty(variable.id);
        if (!variableId) return [];
        return [
          {
            loopNodeId,
            variableId,
            label: `${stringOrEmpty(variable.name) || variableId} · loop variable`,
          },
        ];
      },
    );
  });
  return { workflowInputs, nodeOutputs, loopVariables };
}

export function safeValueBinding(value: unknown): ValueBinding | null {
  const parsed = valueBindingSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
}

const literalKind = (
  value: JsonValue,
): "string" | "number" | "boolean" | "null" | "json" => {
  if (value === null) return "null";
  if (typeof value === "string") return "string";
  if (typeof value === "number") return "number";
  if (typeof value === "boolean") return "boolean";
  return "json";
};

function LiteralBindingEditor({
  disabled,
  onChange,
  value,
}: {
  disabled: boolean;
  onChange: (value: ValueBinding) => void;
  value: JsonValue;
}) {
  const kind = literalKind(value);
  const [jsonText, setJsonText] = useState(() =>
    kind === "json" ? JSON.stringify(value, null, 2) : "{}",
  );
  const [jsonIssue, setJsonIssue] = useState<string | null>(null);

  const changeKind = (
    next: "string" | "number" | "boolean" | "null" | "json",
  ) => {
    const defaults: Record<typeof next, JsonValue> = {
      string: "",
      number: 0,
      boolean: false,
      null: null,
      json: {},
    };
    onChange({ kind: "literal", value: defaults[next] });
  };

  return (
    <div className="space-y-2">
      <select
        aria-label="字面量类型"
        className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
        disabled={disabled}
        onChange={(event) =>
          changeKind(
            event.currentTarget.value as
              | "string"
              | "number"
              | "boolean"
              | "null"
              | "json",
          )
        }
        value={kind}
      >
        <option value="string">string</option>
        <option value="number">number</option>
        <option value="boolean">boolean</option>
        <option value="null">null</option>
        <option value="json">JSON</option>
      </select>
      {kind === "string" ? (
        <Input
          aria-label="string 字面量"
          disabled={disabled}
          onChange={(event) =>
            onChange({ kind: "literal", value: event.currentTarget.value })
          }
          value={value as string}
        />
      ) : null}
      {kind === "number" ? (
        <Input
          aria-label="number 字面量"
          disabled={disabled}
          onChange={(event) => {
            const next = Number(event.currentTarget.value);
            if (
              Number.isFinite(next) &&
              (!Number.isInteger(next) || Number.isSafeInteger(next))
            ) {
              onChange({ kind: "literal", value: next });
            }
          }}
          type="number"
          value={value as number}
        />
      ) : null}
      {kind === "boolean" ? (
        <select
          aria-label="boolean 字面量"
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          disabled={disabled}
          onChange={(event) =>
            onChange({
              kind: "literal",
              value: event.currentTarget.value === "true",
            })
          }
          value={value ? "true" : "false"}
        >
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      ) : null}
      {kind === "null" ? (
        <p className="text-muted-foreground text-xs">JSON null</p>
      ) : null}
      {kind === "json" ? (
        <>
          <Textarea
            aria-invalid={jsonIssue !== null}
            aria-label="JSON 字面量"
            disabled={disabled}
            onChange={(event) => {
              const next = event.currentTarget.value;
              setJsonText(next);
              try {
                const parsed = JSON.parse(next) as JsonValue;
                const checked = valueBindingSchema.safeParse({
                  kind: "literal",
                  value: parsed,
                });
                if (!checked.success) throw new Error("invalid JSON literal");
                setJsonIssue(null);
                onChange(checked.data);
              } catch {
                setJsonIssue("JSON 字面量尚不完整，已保留上一次有效值。");
              }
            }}
            rows={4}
            value={jsonText}
          />
          {jsonIssue ? (
            <p className="text-destructive text-xs" role="alert">
              {jsonIssue}
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

const defaultBindingForKind = (
  kind: ValueBinding["kind"],
  options: TypedBindingOptions,
): ValueBinding => {
  if (kind === "literal") return { kind: "literal", value: null };
  if (kind === "workflow_input") {
    return { kind, input_id: options.workflowInputs[0]?.id ?? "input" };
  }
  if (kind === "loop_variable") {
    return {
      kind,
      loop_node_id:
        options.loopVariables[0]?.loopNodeId ??
        "00000000-0000-4000-8000-000000000000",
      variable_id: options.loopVariables[0]?.variableId ?? "variable",
    };
  }
  return {
    kind,
    node_id:
      options.nodeOutputs[0]?.nodeId ?? "00000000-0000-4000-8000-000000000000",
    output_id: options.nodeOutputs[0]?.outputId ?? "output",
  };
};

export function TypedValueBindingEditor({
  allowedKinds = ["literal", "workflow_input", "node_output", "loop_variable"],
  disabled = false,
  label,
  onChange,
  options = EMPTY_BINDING_OPTIONS,
  value,
}: {
  allowedKinds?: readonly ValueBinding["kind"][];
  disabled?: boolean;
  label: string;
  onChange: (value: ValueBinding) => void;
  options?: TypedBindingOptions;
  value: unknown;
}) {
  const instanceId = useId();
  const parsed = safeValueBinding(value) ?? { kind: "literal", value: null };
  const selectedNodeOutput =
    parsed.kind === "node_output"
      ? `${parsed.node_id}:${parsed.output_id}`
      : "";
  const selectedLoopVariable =
    parsed.kind === "loop_variable"
      ? `${parsed.loop_node_id}:${parsed.variable_id}`
      : "";

  return (
    <fieldset
      aria-label={label}
      className="border-border space-y-2 rounded-md border p-3"
      disabled={disabled}
    >
      <legend className="px-1 text-xs font-medium">{label}</legend>
      <label className="block space-y-1 text-xs" htmlFor={`${instanceId}-kind`}>
        <span>绑定类型</span>
        <select
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          disabled={disabled}
          id={`${instanceId}-kind`}
          onChange={(event) =>
            onChange(
              defaultBindingForKind(
                event.currentTarget.value as ValueBinding["kind"],
                options,
              ),
            )
          }
          value={parsed.kind}
        >
          {allowedKinds.includes("literal") ? (
            <option value="literal">typed literal</option>
          ) : null}
          {allowedKinds.includes("workflow_input") ? (
            <option value="workflow_input">workflow input</option>
          ) : null}
          {allowedKinds.includes("node_output") ? (
            <option value="node_output">node output</option>
          ) : null}
          {allowedKinds.includes("loop_variable") ? (
            <option value="loop_variable">loop variable</option>
          ) : null}
        </select>
      </label>

      {parsed.kind === "literal" ? (
        <LiteralBindingEditor
          disabled={disabled}
          onChange={onChange}
          value={parsed.value}
        />
      ) : null}
      {parsed.kind === "workflow_input" ? (
        options.workflowInputs.length > 0 ? (
          <select
            aria-label="工作流输入"
            className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
            disabled={disabled}
            onChange={(event) =>
              onChange({
                kind: "workflow_input",
                input_id: event.currentTarget.value,
              })
            }
            value={parsed.input_id}
          >
            {!options.workflowInputs.some(
              ({ id }) => id === parsed.input_id,
            ) ? (
              <option value={parsed.input_id}>{parsed.input_id}</option>
            ) : null}
            {options.workflowInputs.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        ) : (
          <Input
            aria-label="工作流输入 ID"
            disabled={disabled}
            onChange={(event) =>
              onChange({
                kind: "workflow_input",
                input_id: event.currentTarget.value,
              })
            }
            value={parsed.input_id}
          />
        )
      ) : null}
      {parsed.kind === "node_output" ? (
        options.nodeOutputs.length > 0 ? (
          <select
            aria-label="节点输出"
            className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
            disabled={disabled}
            onChange={(event) => {
              const option = options.nodeOutputs.find(
                ({ nodeId, outputId }) =>
                  `${nodeId}:${outputId}` === event.currentTarget.value,
              );
              if (option) {
                onChange({
                  kind: "node_output",
                  node_id: option.nodeId,
                  output_id: option.outputId,
                });
              }
            }}
            value={selectedNodeOutput}
          >
            {!options.nodeOutputs.some(
              ({ nodeId, outputId }) =>
                `${nodeId}:${outputId}` === selectedNodeOutput,
            ) ? (
              <option value={selectedNodeOutput}>{selectedNodeOutput}</option>
            ) : null}
            {options.nodeOutputs.map((option) => (
              <option
                key={`${option.nodeId}:${option.outputId}`}
                value={`${option.nodeId}:${option.outputId}`}
              >
                {option.label}
              </option>
            ))}
          </select>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2">
            <Input
              aria-label="节点 ID"
              disabled={disabled}
              onChange={(event) =>
                onChange({ ...parsed, node_id: event.currentTarget.value })
              }
              value={parsed.node_id}
            />
            <Input
              aria-label="输出端口 ID"
              disabled={disabled}
              onChange={(event) =>
                onChange({ ...parsed, output_id: event.currentTarget.value })
              }
              value={parsed.output_id}
            />
          </div>
        )
      ) : null}
      {parsed.kind === "loop_variable" ? (
        options.loopVariables.length > 0 ? (
          <select
            aria-label="循环变量"
            className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
            disabled={disabled}
            onChange={(event) => {
              const option = options.loopVariables.find(
                ({ loopNodeId, variableId }) =>
                  `${loopNodeId}:${variableId}` === event.currentTarget.value,
              );
              if (option) {
                onChange({
                  kind: "loop_variable",
                  loop_node_id: option.loopNodeId,
                  variable_id: option.variableId,
                });
              }
            }}
            value={selectedLoopVariable}
          >
            {!options.loopVariables.some(
              ({ loopNodeId, variableId }) =>
                `${loopNodeId}:${variableId}` === selectedLoopVariable,
            ) ? (
              <option value={selectedLoopVariable}>
                {selectedLoopVariable}
              </option>
            ) : null}
            {options.loopVariables.map((option) => (
              <option
                key={`${option.loopNodeId}:${option.variableId}`}
                value={`${option.loopNodeId}:${option.variableId}`}
              >
                {option.label}
              </option>
            ))}
          </select>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2">
            <Input
              aria-label="循环节点 ID"
              disabled={disabled}
              onChange={(event) =>
                onChange({
                  ...parsed,
                  loop_node_id: event.currentTarget.value,
                })
              }
              value={parsed.loop_node_id}
            />
            <Input
              aria-label="循环变量 ID"
              disabled={disabled}
              onChange={(event) =>
                onChange({
                  ...parsed,
                  variable_id: event.currentTarget.value,
                })
              }
              value={parsed.variable_id}
            />
          </div>
        )
      ) : null}
    </fieldset>
  );
}

export const DEFAULT_PREDICATE_AST: PredicateAst = {
  op: "and",
  items: [
    {
      left: { kind: "literal", value: true },
      operator: "eq",
      right: { kind: "literal", value: true },
    },
  ],
};

export function safePredicateAst(value: unknown): PredicateAst | null {
  const parsed = predicateAstSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
}

const isPredicateGroup = (
  item: PredicateAst | PredicateClause,
): item is PredicateAst => "op" in item && "items" in item;

const operatorsForBinding = (
  binding: ValueBinding,
): PredicateClause["operator"][] => {
  const nullable = ["is_null", "is_not_null"] as const;
  if (binding.kind !== "literal") {
    return [
      "eq",
      "ne",
      "gt",
      "gte",
      "lt",
      "lte",
      "contains",
      "starts_with",
      "ends_with",
      ...nullable,
    ];
  }
  if (typeof binding.value === "number") {
    return ["eq", "ne", "gt", "gte", "lt", "lte", ...nullable];
  }
  if (typeof binding.value === "string") {
    return [
      "eq",
      "ne",
      "gt",
      "gte",
      "lt",
      "lte",
      "contains",
      "starts_with",
      "ends_with",
      ...nullable,
    ];
  }
  return ["eq", "ne", ...nullable];
};

function PredicateClauseEditor({
  allowedBindingKinds,
  clause,
  disabled,
  label,
  onChange,
  options,
}: {
  allowedBindingKinds: readonly ValueBinding["kind"][];
  clause: PredicateClause;
  disabled: boolean;
  label: string;
  onChange: (value: PredicateClause) => void;
  options: TypedBindingOptions;
}) {
  const operators = operatorsForBinding(clause.left);
  const unary =
    clause.operator === "is_null" || clause.operator === "is_not_null";
  return (
    <div className="bg-muted/40 space-y-2 rounded-md p-3">
      <p className="text-xs font-medium">{label}</p>
      <TypedValueBindingEditor
        allowedKinds={allowedBindingKinds}
        disabled={disabled}
        label="左值 binding"
        onChange={(left) => {
          const allowed = operatorsForBinding(left);
          const operator = allowed.includes(clause.operator)
            ? clause.operator
            : allowed[0]!;
          onChange({
            ...clause,
            left,
            operator,
            ...(operator === "is_null" || operator === "is_not_null"
              ? { right: undefined }
              : { right: clause.right ?? { kind: "literal", value: null } }),
          });
        }}
        options={options}
        value={clause.left}
      />
      <label className="block space-y-1 text-xs">
        <span>类型感知 operator</span>
        <select
          aria-label={`${label} operator`}
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          disabled={disabled}
          onChange={(event) => {
            const operator = event.currentTarget
              .value as PredicateClause["operator"];
            onChange({
              ...clause,
              operator,
              ...(operator === "is_null" || operator === "is_not_null"
                ? { right: undefined }
                : {
                    right: clause.right ?? {
                      kind: "literal",
                      value: null,
                    },
                  }),
            });
          }}
          value={clause.operator}
        >
          {operators.map((operator) => (
            <option key={operator} value={operator}>
              {operator}
            </option>
          ))}
        </select>
      </label>
      {!unary ? (
        <TypedValueBindingEditor
          allowedKinds={allowedBindingKinds}
          disabled={disabled}
          label="右值 typed literal / binding"
          onChange={(right) => onChange({ ...clause, right })}
          options={options}
          value={clause.right}
        />
      ) : null}
    </div>
  );
}

function PredicateGroupEditor({
  allowedBindingKinds,
  depth,
  disabled,
  group,
  label,
  onChange,
  options,
}: {
  allowedBindingKinds: readonly ValueBinding["kind"][];
  depth: number;
  disabled: boolean;
  group: PredicateAst;
  label: string;
  onChange: (value: PredicateAst) => void;
  options: TypedBindingOptions;
}) {
  const replaceItem = (
    index: number,
    value: PredicateAst | PredicateClause,
  ) => {
    const items = [...group.items];
    items[index] = value;
    onChange({ ...group, items });
  };
  return (
    <fieldset
      aria-label={label}
      className="border-border space-y-3 rounded-md border p-3"
      disabled={disabled}
    >
      <legend className="px-1 text-xs font-medium">{label}</legend>
      <label className="block space-y-1 text-xs">
        <span>逻辑分组</span>
        <select
          aria-label={`${label} AND OR`}
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          disabled={disabled}
          onChange={(event) =>
            onChange({
              ...group,
              op: event.currentTarget.value as PredicateAst["op"],
            })
          }
          value={group.op}
        >
          <option value="and">AND</option>
          <option value="or">OR</option>
        </select>
      </label>
      {group.items.map((item, index) => (
        <div className="space-y-2" key={`${depth}:${index}`}>
          {isPredicateGroup(item) ? (
            <PredicateGroupEditor
              allowedBindingKinds={allowedBindingKinds}
              depth={depth + 1}
              disabled={disabled}
              group={item}
              label={`嵌套分组 ${index + 1}`}
              onChange={(value) => replaceItem(index, value)}
              options={options}
            />
          ) : (
            <PredicateClauseEditor
              allowedBindingKinds={allowedBindingKinds}
              clause={item}
              disabled={disabled}
              label={`条件 ${index + 1}`}
              onChange={(value) => replaceItem(index, value)}
              options={options}
            />
          )}
          <Button
            aria-label={`删除${label}第 ${index + 1} 项`}
            disabled={disabled || group.items.length <= 1}
            onClick={() =>
              onChange({
                ...group,
                items: group.items.filter(
                  (_, itemIndex) => itemIndex !== index,
                ),
              })
            }
            size="sm"
            type="button"
            variant="ghost"
          >
            删除此项
          </Button>
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button
          disabled={disabled}
          onClick={() =>
            onChange({
              ...group,
              items: [
                ...group.items,
                {
                  left: { kind: "literal", value: true },
                  operator: "eq",
                  right: { kind: "literal", value: true },
                },
              ],
            })
          }
          size="sm"
          type="button"
          variant="outline"
        >
          添加 typed 条件
        </Button>
        <Button
          disabled={disabled}
          onClick={() =>
            onChange({
              ...group,
              items: [...group.items, structuredClone(DEFAULT_PREDICATE_AST)],
            })
          }
          size="sm"
          type="button"
          variant="outline"
        >
          添加 AND/OR 分组
        </Button>
      </div>
    </fieldset>
  );
}

export function PredicateAstEditor({
  allowedBindingKinds = [
    "literal",
    "workflow_input",
    "node_output",
    "loop_variable",
  ],
  disabled = false,
  label,
  onChange,
  options = EMPTY_BINDING_OPTIONS,
  value,
}: {
  allowedBindingKinds?: readonly ValueBinding["kind"][];
  disabled?: boolean;
  label: string;
  onChange: (value: PredicateAst) => void;
  options?: TypedBindingOptions;
  value: unknown;
}) {
  const parsed = safePredicateAst(value) ?? DEFAULT_PREDICATE_AST;
  return (
    <div className="space-y-2">
      <p className="text-muted-foreground text-xs">
        typed Predicate AST；仅支持 AND/OR、类型化 operator 与 typed binding。
      </p>
      <PredicateGroupEditor
        allowedBindingKinds={allowedBindingKinds}
        depth={0}
        disabled={disabled}
        group={parsed}
        label={label}
        onChange={onChange}
        options={options}
      />
    </div>
  );
}
