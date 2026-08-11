"use client";

import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import type { WorkflowWorkbenchCommand } from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import {
  BasicBindingEditor,
  BasicJsonEditor,
  BasicPanelField,
  BasicRestrictedTemplateEditor,
  stableBasicRowId,
} from "../basic/shared";

import {
  httpHeaderNameIsSafe,
  httpMethodIsWrite,
  httpQueryNameIsSafe,
} from "./http-node-config-helpers";

export const isHttpRecord = (
  value: unknown,
): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

export const httpRecord = (value: unknown): Record<string, unknown> =>
  isHttpRecord(value) ? value : {};

export const httpString = (value: unknown): string =>
  typeof value === "string" ? value : "";

const httpRows = (value: unknown): Record<string, unknown>[] =>
  Array.isArray(value) ? value.filter(isHttpRecord) : [];

const safeRow = (
  row: Record<string, unknown>,
  patch: Record<string, unknown>,
): Record<string, unknown> => {
  const merged = { ...row, ...patch };
  return {
    ...(typeof merged.id === "string" ? { id: merged.id } : {}),
    ...(typeof merged.name === "string" ? { name: merged.name } : {}),
    ...(merged.value !== undefined ? { value: merged.value } : {}),
  };
};

const rowValueMode = (value: unknown): "binding" | "template" => {
  const record = httpRecord(value);
  return record.version === 1 && Array.isArray(record.segments)
    ? "template"
    : "binding";
};

export function HttpKeyValueRowsEditor({
  kind,
  label,
  onChange,
  value,
}: {
  kind: "query" | "header" | "form";
  label: string;
  onChange: (rows: Record<string, unknown>[]) => void;
  value: unknown;
}) {
  const rows = httpRows(value);
  const replace = (index: number, patch: Record<string, unknown>) =>
    onChange(
      rows.map((row, rowIndex) =>
        rowIndex === index ? safeRow(row, patch) : safeRow(row, {}),
      ),
    );
  const move = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= rows.length) return;
    const next = rows.map((row) => safeRow(row, {}));
    [next[index], next[target]] = [next[target]!, next[index]!];
    onChange(next);
  };
  return (
    <section aria-label={label} className="space-y-3">
      <h4 className="text-sm font-medium">{label}</h4>
      {rows.length === 0 ? (
        <p className="text-muted-foreground text-xs">尚未配置。</p>
      ) : null}
      {rows.map((row, index) => {
        const id = httpString(row.id) || `${kind}-${index}`;
        const name = httpString(row.name);
        const validName =
          kind === "header"
            ? httpHeaderNameIsSafe(name)
            : kind === "query"
              ? httpQueryNameIsSafe(name)
              : name.length > 0;
        const mode = rowValueMode(row.value);
        return (
          <section
            aria-label={`${label} ${index + 1}`}
            className="border-border space-y-3 rounded-md border p-3"
            key={id}
          >
            <input type="hidden" value={id} />
            <div className="grid gap-2 sm:grid-cols-[1fr_auto_auto]">
              <Input
                aria-invalid={!validName}
                aria-label={`${label} ${index + 1} 名称`}
                onChange={(event) =>
                  replace(index, { name: event.currentTarget.value })
                }
                value={name}
              />
              <select
                aria-label={`${label} ${index + 1} 值类型`}
                className="border-input h-9 rounded-md border bg-transparent px-2 text-sm"
                onChange={(event) =>
                  replace(index, {
                    value:
                      event.currentTarget.value === "template"
                        ? { version: 1, segments: [] }
                        : { kind: "literal", value: "" },
                  })
                }
                value={mode}
              >
                <option value="binding">Binding</option>
                <option value="template">受限模板</option>
              </select>
              <Button
                onClick={() =>
                  onChange(rows.filter((_, rowIndex) => rowIndex !== index))
                }
                type="button"
                variant="ghost"
              >
                删除
              </Button>
            </div>
            {!validName && name.length > 0 ? (
              <p className="text-destructive text-xs" role="alert">
                名称为空、像秘密或属于传输层受控字段。
              </p>
            ) : null}
            {mode === "template" ? (
              <BasicRestrictedTemplateEditor
                label={`${label} ${index + 1} 值模板`}
                onChange={(next) => replace(index, { value: next })}
                value={row.value}
              />
            ) : (
              <BasicBindingEditor
                label={`${label} ${index + 1} 值`}
                onChange={(next) =>
                  replace(index, { value: next ?? undefined })
                }
                value={row.value}
              />
            )}
            <div className="flex gap-2">
              <Button
                disabled={index === 0}
                onClick={() => move(index, -1)}
                size="sm"
                type="button"
                variant="ghost"
              >
                上移
              </Button>
              <Button
                disabled={index === rows.length - 1}
                onClick={() => move(index, 1)}
                size="sm"
                type="button"
                variant="ghost"
              >
                下移
              </Button>
            </div>
          </section>
        );
      })}
      <Button
        onClick={() =>
          onChange([
            ...rows.map((row) => safeRow(row, {})),
            {
              id: stableBasicRowId(kind),
              name: "",
              value: { kind: "literal", value: "" },
            },
          ])
        }
        size="sm"
        type="button"
        variant="outline"
      >
        添加{label}
      </Button>
    </section>
  );
}

export function HttpRestrictedJsonTemplateEditor({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (template: Record<string, unknown>) => void;
  value: unknown;
}) {
  const template = httpRecord(value);
  const bindings = httpRecord(template.bindings);
  return (
    <section aria-label={label} className="space-y-3">
      <h4 className="text-sm font-medium">{label}</h4>
      <BasicJsonEditor
        label="JSON 模板"
        onChange={(next) => onChange({ version: 1, template: next, bindings })}
        value={Object.hasOwn(template, "template") ? template.template : {}}
      />
      {Object.entries(bindings).map(([id, binding]) => (
        <BasicBindingEditor
          key={id}
          label={`JSON 模板变量 ${id}`}
          onChange={(next) => {
            const updated = { ...bindings };
            if (next === null) delete updated[id];
            else updated[id] = next;
            onChange({
              version: 1,
              template: Object.hasOwn(template, "template")
                ? template.template
                : {},
              bindings: updated,
            });
          }}
          value={binding}
        />
      ))}
      <Button
        onClick={() => {
          const id = stableBasicRowId("json_binding");
          onChange({
            version: 1,
            template: Object.hasOwn(template, "template")
              ? template.template
              : {},
            bindings: {
              ...bindings,
              [id]: { kind: "workflow_input", input_id: "" },
            },
          });
        }}
        size="sm"
        type="button"
        variant="outline"
      >
        添加 JSON 模板变量
      </Button>
    </section>
  );
}

type UpdateExecutionPolicyCommand = Extract<
  WorkflowWorkbenchCommand,
  { type: "update_node_execution_policy" }
>;

export function HttpExecutionPolicyEditor({
  method,
  node,
  onCommand,
}: {
  method: unknown;
  node: WorkflowNodeConfigPanelProps["node"];
  onCommand: (command: UpdateExecutionPolicyCommand) => void;
}) {
  const policy = httpRecord(node.execution_policy);
  const retry = httpRecord(policy.retry);
  const onError = httpRecord(policy.on_error);
  const writeMethod = httpMethodIsWrite(method);
  const commit = (patch: Record<string, unknown>) =>
    onCommand({
      type: "update_node_execution_policy",
      node_id: typeof node.id === "string" ? node.id : "",
      execution_policy: { ...policy, ...patch },
    } as UpdateExecutionPolicyCommand);

  return (
    <section aria-label="HTTP 执行策略" className="space-y-3">
      <h4 className="text-sm font-medium">Retry 与异常处理</h4>
      {writeMethod ? (
        <div className="space-y-2">
          <p className="text-muted-foreground text-xs">
            写方法默认禁止客户端开启 Retry；只有冻结 endpoint policy
            明确支持服务端幂等键时，服务端才可能批准重试。
          </p>
          {retry.mode === "bounded" ? (
            <>
              <p className="text-destructive text-xs" role="alert">
                当前 Draft 仍请求 bounded retry；发布前必须重置为 none。
              </p>
              <Button
                onClick={() => commit({ retry: { mode: "none" } })}
                size="sm"
                type="button"
                variant="outline"
              >
                重置为不重试
              </Button>
            </>
          ) : null}
        </div>
      ) : (
        <>
          <BasicPanelField label="读请求重试">
            <select
              aria-label="HTTP 重试模式"
              className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
              onChange={(event) =>
                commit({
                  retry:
                    event.currentTarget.value === "bounded"
                      ? {
                          mode: "bounded",
                          max_attempts: 2,
                          backoff_ms: 1_000,
                        }
                      : { mode: "none" },
                })
              }
              value={retry.mode === "bounded" ? "bounded" : "none"}
            >
              <option value="none">不重试</option>
              <option value="bounded">服务端有界重试请求</option>
            </select>
          </BasicPanelField>
          {retry.mode === "bounded" ? (
            <div className="grid gap-2 sm:grid-cols-2">
              <Input
                aria-label="HTTP 最大尝试次数"
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
                  typeof retry.max_attempts === "number"
                    ? retry.max_attempts
                    : 2
                }
              />
              <Input
                aria-label="HTTP 退避毫秒"
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
                  typeof retry.backoff_ms === "number"
                    ? retry.backoff_ms
                    : 1_000
                }
              />
            </div>
          ) : null}
        </>
      )}
      <BasicPanelField label="可安全分类的错误">
        <select
          aria-label="HTTP 错误处理模式"
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
          value={httpString(onError.mode) || "fail_workflow"}
        >
          <option value="fail_workflow">工作流失败</option>
          <option value="route_error">路由到 error</option>
          <option value="continue_with_typed_default">使用类型化默认值</option>
        </select>
      </BasicPanelField>
      <p className="text-muted-foreground text-xs">
        side_effect_unknown、安全策略失败、取消与 lease loss 永不作为普通 error
        route。
      </p>
    </section>
  );
}
