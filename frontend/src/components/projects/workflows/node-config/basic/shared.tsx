"use client";

import type { ChangeEvent, ReactNode } from "react";

import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import type {
  WorkflowWorkbenchCommand,
  WorkflowWorkbenchStorePort,
} from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { JsonValue } from "@/core/project-workflows/types";

export type BasicDraftNode = WorkflowNodeConfigPanelProps["node"];
export type BasicPanelLock = Pick<
  WorkflowNodeConfigPanelProps,
  "disabled" | "readOnly"
>;

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

export const recordOrEmpty = (value: unknown): Record<string, unknown> =>
  isRecord(value) ? value : {};

export const arrayOrEmpty = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];

export const stringOrEmpty = (value: unknown): string =>
  typeof value === "string" ? value : "";

export const booleanOrFalse = (value: unknown): boolean => value === true;

export const basicPanelLocked = ({ disabled, readOnly }: BasicPanelLock) =>
  disabled || readOnly;

export const basicPanelWriteDisabled = (
  props: WorkflowNodeConfigPanelProps,
): boolean =>
  props.disabled ||
  props.readOnly ||
  !props.capabilities.includes("workflow.edit") ||
  props.catalogEntry.availability.state !== "enabled";

export const nodeConfigOrEmpty = (node: BasicDraftNode) =>
  recordOrEmpty(node.config);

export const stableBasicRowId = (prefix: string): string => {
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "");
  return `${prefix}_${random?.slice(0, 16) ?? "new"}`;
};

type UpdateNodeInputBindingsCommand = Extract<
  WorkflowWorkbenchCommand,
  { type: "update_node_input_bindings" }
>;
type UpdateNodeExecutionPolicyCommand = Extract<
  WorkflowWorkbenchCommand,
  { type: "update_node_execution_policy" }
>;

export function buildBasicNodeInputBindingsUpdate(
  node: BasicDraftNode,
  inputBindings: Record<string, unknown>,
): UpdateNodeInputBindingsCommand {
  return {
    type: "update_node_input_bindings",
    node_id: stringOrEmpty(node.id),
    input_bindings: structuredClone(inputBindings),
  } as UpdateNodeInputBindingsCommand;
}

export function buildBasicNodeExecutionPolicyUpdate(
  node: BasicDraftNode,
  executionPolicy: Record<string, unknown>,
): UpdateNodeExecutionPolicyCommand {
  return {
    type: "update_node_execution_policy",
    node_id: stringOrEmpty(node.id),
    execution_policy: structuredClone(executionPolicy),
  } as UpdateNodeExecutionPolicyCommand;
}

export function dispatchBasicCommand(
  store: WorkflowWorkbenchStorePort,
  command: WorkflowWorkbenchCommand,
): void {
  store.dispatch(command);
}

export function BasicPanelField({
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

export function BasicInlineIssues({ issues }: { issues: readonly string[] }) {
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

export function BasicPanelShell({
  children,
  disabled,
  issues,
  readOnly,
  title,
}: BasicPanelLock & {
  children: ReactNode;
  issues: readonly string[];
  title: string;
}) {
  const locked = basicPanelLocked({ disabled, readOnly });
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
      <BasicInlineIssues issues={issues} />
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

export type BasicValueType = {
  kind: "string" | "number" | "boolean" | "json" | "messages";
  collection: boolean;
  nullable: boolean;
  schema_ref?: string;
};

export const safeValueType = (value: unknown): BasicValueType => {
  const record = recordOrEmpty(value);
  const kind = ["string", "number", "boolean", "json", "messages"].includes(
    stringOrEmpty(record.kind),
  )
    ? (record.kind as BasicValueType["kind"])
    : "string";
  return {
    kind,
    collection: record.collection === true,
    nullable: record.nullable === true,
    ...(typeof record.schema_ref === "string"
      ? { schema_ref: record.schema_ref }
      : {}),
  };
};

export function BasicValueTypeEditor({
  onChange,
  value,
}: {
  onChange: (value: BasicValueType) => void;
  value: unknown;
}) {
  const parsed = safeValueType(value);
  return (
    <div className="grid gap-2 sm:grid-cols-3">
      <select
        aria-label="值类型"
        className="border-input h-9 rounded-md border bg-transparent px-2 text-sm"
        onChange={(event) =>
          onChange({
            ...parsed,
            kind: event.currentTarget.value as BasicValueType["kind"],
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
      <label className="flex items-center gap-2 text-xs">
        <input
          checked={parsed.collection}
          onChange={(event) =>
            onChange({ ...parsed, collection: event.currentTarget.checked })
          }
          type="checkbox"
        />
        数组
      </label>
      <label className="flex items-center gap-2 text-xs">
        <input
          checked={parsed.nullable}
          onChange={(event) =>
            onChange({ ...parsed, nullable: event.currentTarget.checked })
          }
          type="checkbox"
        />
        可为 null
      </label>
    </div>
  );
}

export const safeJsonText = (value: unknown, fallback = ""): string => {
  if (value === undefined) return fallback;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return fallback;
  }
};

export const parseJsonValue = (
  text: string,
): { success: true; value: JsonValue } | { success: false } => {
  try {
    const value = JSON.parse(text) as JsonValue;
    return { success: true, value };
  } catch {
    return { success: false };
  }
};

export function BasicJsonEditor({
  label,
  objectOnly = false,
  onChange,
  value,
}: {
  label: string;
  objectOnly?: boolean;
  onChange: (value: JsonValue) => void;
  value: unknown;
}) {
  const commit = (event: ChangeEvent<HTMLTextAreaElement>) => {
    const parsed = parseJsonValue(event.currentTarget.value);
    if (
      parsed.success &&
      (!objectOnly ||
        (parsed.value !== null &&
          typeof parsed.value === "object" &&
          !Array.isArray(parsed.value)))
    ) {
      onChange(parsed.value);
    }
  };
  return (
    <BasicPanelField
      help="仅接受严格 JSON；无效文本不会写入 Draft。"
      label={label}
    >
      <Textarea
        aria-label={label}
        defaultValue={safeJsonText(value, objectOnly ? "{}" : "null")}
        onBlur={commit}
        spellCheck={false}
      />
    </BasicPanelField>
  );
}

type BindingKind =
  | "unbound"
  | "literal"
  | "workflow_input"
  | "node_output"
  | "loop_variable";

const bindingKind = (value: unknown): BindingKind => {
  const kind = recordOrEmpty(value).kind;
  return ["literal", "workflow_input", "node_output", "loop_variable"].includes(
    stringOrEmpty(kind),
  )
    ? (kind as BindingKind)
    : "unbound";
};

export function BasicBindingEditor({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (value: Record<string, unknown> | null) => void;
  value: unknown;
}) {
  const record = recordOrEmpty(value);
  const kind = bindingKind(value);
  const chooseKind = (next: BindingKind) => {
    const defaults: Record<BindingKind, Record<string, unknown> | null> = {
      unbound: null,
      literal: { kind: "literal", value: null },
      workflow_input: { kind: "workflow_input", input_id: "" },
      node_output: { kind: "node_output", node_id: "", output_id: "" },
      loop_variable: {
        kind: "loop_variable",
        loop_node_id: "",
        variable_id: "",
      },
    };
    onChange(defaults[next]);
  };
  return (
    <div className="border-border space-y-2 rounded-md border p-3">
      <BasicPanelField label={label}>
        <select
          aria-label={`${label}绑定类型`}
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          onChange={(event) =>
            chooseKind(event.currentTarget.value as BindingKind)
          }
          value={kind}
        >
          <option value="unbound">未绑定</option>
          <option value="literal">固定值</option>
          <option value="workflow_input">工作流输入</option>
          <option value="node_output">节点输出</option>
          <option value="loop_variable">循环变量</option>
        </select>
      </BasicPanelField>
      {kind === "literal" ? (
        <BasicJsonEditor
          label="固定 JSON 值"
          onChange={(next) => onChange({ kind: "literal", value: next })}
          value={record.value}
        />
      ) : null}
      {kind === "workflow_input" ? (
        <Input
          aria-label="工作流输入 ID"
          onChange={(event) =>
            onChange({
              kind: "workflow_input",
              input_id: event.currentTarget.value,
            })
          }
          value={stringOrEmpty(record.input_id)}
        />
      ) : null}
      {kind === "node_output" ? (
        <div className="grid gap-2 sm:grid-cols-2">
          <Input
            aria-label="来源节点 ID"
            onChange={(event) =>
              onChange({
                kind: "node_output",
                node_id: event.currentTarget.value,
                output_id: stringOrEmpty(record.output_id),
                ...(typeof record.path === "string"
                  ? { path: record.path }
                  : {}),
              })
            }
            value={stringOrEmpty(record.node_id)}
          />
          <Input
            aria-label="来源输出 ID"
            onChange={(event) =>
              onChange({
                kind: "node_output",
                node_id: stringOrEmpty(record.node_id),
                output_id: event.currentTarget.value,
                ...(typeof record.path === "string"
                  ? { path: record.path }
                  : {}),
              })
            }
            value={stringOrEmpty(record.output_id)}
          />
        </div>
      ) : null}
      {kind === "loop_variable" ? (
        <div className="grid gap-2 sm:grid-cols-2">
          <Input
            aria-label="循环节点 ID"
            onChange={(event) =>
              onChange({
                kind: "loop_variable",
                loop_node_id: event.currentTarget.value,
                variable_id: stringOrEmpty(record.variable_id),
              })
            }
            value={stringOrEmpty(record.loop_node_id)}
          />
          <Input
            aria-label="循环变量 ID"
            onChange={(event) =>
              onChange({
                kind: "loop_variable",
                loop_node_id: stringOrEmpty(record.loop_node_id),
                variable_id: event.currentTarget.value,
              })
            }
            value={stringOrEmpty(record.variable_id)}
          />
        </div>
      ) : null}
    </div>
  );
}

type RestrictedTemplate = {
  version: 1;
  segments: Array<Record<string, unknown>>;
};

export const safeRestrictedTemplate = (value: unknown): RestrictedTemplate => {
  const record = recordOrEmpty(value);
  const segments = arrayOrEmpty(record.segments)
    .filter(isRecord)
    .filter((segment) => segment.kind === "text" || segment.kind === "binding");
  return { version: 1, segments };
};

export function BasicRestrictedTemplateEditor({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (value: RestrictedTemplate) => void;
  value: unknown;
}) {
  const template = safeRestrictedTemplate(value);
  const replace = (index: number, segment: Record<string, unknown>) =>
    onChange({
      version: 1,
      segments: template.segments.map((item, itemIndex) =>
        itemIndex === index ? segment : item,
      ),
    });
  const remove = (index: number) =>
    onChange({
      version: 1,
      segments: template.segments.filter((_, itemIndex) => itemIndex !== index),
    });
  return (
    <section aria-label={label} className="space-y-2">
      <h4 className="text-sm font-medium">{label}</h4>
      {template.segments.length === 0 ? (
        <p className="text-muted-foreground text-xs">尚未添加模板片段。</p>
      ) : null}
      {template.segments.map((segment, index) =>
        segment.kind === "text" ? (
          <div
            className="border-border space-y-2 rounded-md border p-3"
            key={`text-${index}`}
          >
            <Textarea
              aria-label={`${label}文本 ${index + 1}`}
              onChange={(event) =>
                replace(index, {
                  kind: "text",
                  value: event.currentTarget.value,
                })
              }
              value={stringOrEmpty(segment.value)}
            />
            <Button
              onClick={() => remove(index)}
              size="sm"
              type="button"
              variant="ghost"
            >
              删除片段
            </Button>
          </div>
        ) : (
          <div className="space-y-2" key={`binding-${index}`}>
            <BasicBindingEditor
              label={`${label}变量 ${index + 1}`}
              onChange={(binding) =>
                binding === null
                  ? remove(index)
                  : replace(index, { kind: "binding", value: binding })
              }
              value={segment.value}
            />
            <Button
              onClick={() => remove(index)}
              size="sm"
              type="button"
              variant="ghost"
            >
              删除片段
            </Button>
          </div>
        ),
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={() =>
            onChange({
              version: 1,
              segments: [...template.segments, { kind: "text", value: "" }],
            })
          }
          size="sm"
          type="button"
          variant="outline"
        >
          添加文本
        </Button>
        <Button
          onClick={() =>
            onChange({
              version: 1,
              segments: [
                ...template.segments,
                {
                  kind: "binding",
                  value: { kind: "workflow_input", input_id: "" },
                },
              ],
            })
          }
          size="sm"
          type="button"
          variant="outline"
        >
          添加变量
        </Button>
      </div>
    </section>
  );
}

export function BasicNodeInputBindingsEditor({
  items,
  node,
  onCommand,
}: {
  items: readonly { id: string; label: string }[];
  node: BasicDraftNode;
  onCommand: (command: UpdateNodeInputBindingsCommand) => void;
}) {
  const bindings = recordOrEmpty(node.input_bindings);
  return (
    <section aria-label="输入绑定" className="space-y-2">
      <h4 className="text-sm font-medium">输入变量与绑定</h4>
      {items.length === 0 ? (
        <p className="text-muted-foreground text-xs">尚未声明输入变量。</p>
      ) : null}
      {items.map((item) => (
        <BasicBindingEditor
          key={item.id}
          label={item.label}
          onChange={(binding) =>
            onCommand(
              buildBasicNodeInputBindingsUpdate(node, {
                ...bindings,
                [item.id]: binding,
              }),
            )
          }
          value={bindings[item.id]}
        />
      ))}
    </section>
  );
}

export function BasicExecutionPolicyEditor({
  node,
  onCommand,
}: {
  node: BasicDraftNode;
  onCommand: (command: UpdateNodeExecutionPolicyCommand) => void;
}) {
  const policy = recordOrEmpty(node.execution_policy);
  const retry = recordOrEmpty(policy.retry);
  const onError = recordOrEmpty(policy.on_error);
  const commit = (patch: Record<string, unknown>) =>
    onCommand(
      buildBasicNodeExecutionPolicyUpdate(node, { ...policy, ...patch }),
    );
  return (
    <section aria-label="执行策略" className="space-y-3">
      <h4 className="text-sm font-medium">执行策略</h4>
      <BasicPanelField label="重试">
        <select
          aria-label="重试模式"
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          onChange={(event) =>
            commit({
              retry:
                event.currentTarget.value === "bounded"
                  ? { mode: "bounded", max_attempts: 2, backoff_ms: 1_000 }
                  : { mode: "none" },
            })
          }
          value={retry.mode === "bounded" ? "bounded" : "none"}
        >
          <option value="none">不重试</option>
          <option value="bounded">有界重试</option>
        </select>
      </BasicPanelField>
      {retry.mode === "bounded" ? (
        <div className="grid gap-2 sm:grid-cols-2">
          <Input
            aria-label="最大尝试次数"
            min={1}
            onChange={(event) =>
              commit({
                retry: {
                  ...retry,
                  mode: "bounded",
                  max_attempts: Number(event.currentTarget.value),
                },
              })
            }
            type="number"
            value={
              typeof retry.max_attempts === "number" ? retry.max_attempts : 2
            }
          />
          <Input
            aria-label="退避毫秒"
            min={0}
            onChange={(event) =>
              commit({
                retry: {
                  ...retry,
                  mode: "bounded",
                  backoff_ms: Number(event.currentTarget.value),
                },
              })
            }
            type="number"
            value={
              typeof retry.backoff_ms === "number" ? retry.backoff_ms : 1_000
            }
          />
        </div>
      ) : null}
      <BasicPanelField label="错误处理">
        <select
          aria-label="错误处理模式"
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          onChange={(event) => {
            const mode = event.currentTarget.value;
            commit({
              on_error:
                mode === "route_error"
                  ? { mode, output_port_id: "error" }
                  : mode === "continue_with_typed_default"
                    ? { mode, value: null }
                    : { mode: "fail_workflow" },
            });
          }}
          value={stringOrEmpty(onError.mode) || "fail_workflow"}
        >
          <option value="fail_workflow">工作流失败</option>
          <option value="route_error">路由到 error</option>
          <option value="continue_with_typed_default">使用类型化默认值</option>
        </select>
      </BasicPanelField>
    </section>
  );
}
