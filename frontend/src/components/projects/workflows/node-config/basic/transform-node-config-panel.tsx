"use client";

import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import { useWorkflowWorkbenchStore } from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { transformNodeConfigV1Schema } from "@/core/project-workflows/types";

import {
  BasicBindingEditor,
  BasicJsonEditor,
  BasicNodeInputBindingsEditor,
  BasicPanelField,
  BasicPanelShell,
  BasicRestrictedTemplateEditor,
  BasicValueTypeEditor,
  basicPanelWriteDisabled,
  dispatchBasicCommand,
  isRecord,
  nodeConfigOrEmpty,
  recordOrEmpty,
  stableBasicRowId,
  stringOrEmpty,
  type BasicDraftNode,
} from "./shared";

const TRANSFORM_CONFIG_FIELDS = new Set([
  "mode",
  "input_variables",
  "missing_variable",
  "template",
  "output_schema",
]);

export function buildTransformNodeConfigUpdate(
  node: BasicDraftNode,
  patch: Record<string, unknown>,
) {
  const current = nodeConfigOrEmpty(node);
  const config = Object.fromEntries(
    Object.entries(current).filter(([field]) =>
      TRANSFORM_CONFIG_FIELDS.has(field),
    ),
  );
  for (const [field, value] of Object.entries(patch)) {
    if (TRANSFORM_CONFIG_FIELDS.has(field)) {
      config[field] = structuredClone(value);
    }
  }
  return {
    type: "update_node_config" as const,
    node_id: stringOrEmpty(node.id),
    config,
  };
}

const transformVariables = (config: Record<string, unknown>) =>
  Array.isArray(config.input_variables)
    ? config.input_variables.filter(isRecord)
    : [];

function transformIssues(config: Record<string, unknown>): string[] {
  const issues: string[] = [];
  const mode = config.mode;
  const variables = transformVariables(config);
  const names = new Set<string>();
  variables.forEach((variable, index) => {
    const name = stringOrEmpty(variable.name);
    if (!name) issues.push(`输入变量 ${index + 1} 缺少名称。`);
    if (name && names.has(name)) issues.push(`输入变量名 ${name} 重复。`);
    names.add(name);
  });
  if (mode !== "text" && mode !== "json") {
    issues.push("请选择 text 或 json 转换模式。");
  }
  if (!isRecord(config.template)) {
    issues.push("受限模板尚未配置。");
  }
  if (mode === "json" && !isRecord(config.output_schema)) {
    issues.push("JSON 输出 Schema 未配置。");
  }
  if (mode === "text" && config.output_schema !== null) {
    issues.push("Text 模式的 output_schema 必须为 null。");
  }
  if (
    Object.keys(config).some((field) => !TRANSFORM_CONFIG_FIELDS.has(field))
  ) {
    issues.push("Transform 配置包含不支持的字段；这些字段不会显示或写回。");
  }
  if (!transformNodeConfigV1Schema.safeParse(config).success) {
    issues.push("Transform Draft 尚未满足发布合同，可继续补齐标记字段。");
  }
  return [...new Set(issues)];
}

const textModeShape = (config: Record<string, unknown>) => ({
  mode: "text",
  input_variables: transformVariables(config),
  missing_variable: ["error", "null", "empty"].includes(
    stringOrEmpty(config.missing_variable),
  )
    ? config.missing_variable
    : "error",
  template:
    config.mode === "text" &&
    isRecord(config.template) &&
    config.template.version === 1 &&
    Array.isArray(config.template.segments)
      ? config.template
      : { version: 1, segments: [] },
  output_schema: null,
});

const jsonModeShape = (config: Record<string, unknown>) => {
  const rawTemplate = recordOrEmpty(config.template);
  return {
    mode: "json",
    input_variables: transformVariables(config),
    missing_variable: ["error", "null", "empty"].includes(
      stringOrEmpty(config.missing_variable),
    )
      ? config.missing_variable
      : "error",
    template:
      config.mode === "json" &&
      rawTemplate.version === 1 &&
      Object.hasOwn(rawTemplate, "template")
        ? rawTemplate
        : { version: 1, template: {}, bindings: {} },
    output_schema: isRecord(config.output_schema) ? config.output_schema : {},
  };
};

export function TransformNodeConfigPanel(props: WorkflowNodeConfigPanelProps) {
  const store = useWorkflowWorkbenchStore();
  const config = nodeConfigOrEmpty(props.node);
  const variables = transformVariables(config);
  const mode = config.mode === "json" ? "json" : "text";
  const locked = basicPanelWriteDisabled(props);
  const commit = (patch: Record<string, unknown>) =>
    dispatchBasicCommand(
      store,
      buildTransformNodeConfigUpdate(props.node, patch),
    );
  const replaceVariable = (index: number, patch: Record<string, unknown>) =>
    commit({
      input_variables: variables.map((variable, variableIndex) =>
        variableIndex === index ? { ...variable, ...patch } : variable,
      ),
    });
  const template = recordOrEmpty(config.template);
  const templateBindings = recordOrEmpty(template.bindings);

  return (
    <BasicPanelShell
      disabled={locked && !props.readOnly}
      issues={transformIssues(config)}
      readOnly={props.readOnly}
      title="模板转换配置"
    >
      <p className="text-muted-foreground text-xs">
        使用受限模板 AST；不执行 Jinja、函数调用、import、交互 HTML
        或任意表达式。
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <BasicPanelField label="模式">
          <select
            aria-label="Transform 模式"
            className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
            onChange={(event) =>
              dispatchBasicCommand(store, {
                type: "update_node_config",
                node_id: props.nodeId,
                config:
                  event.currentTarget.value === "json"
                    ? jsonModeShape(config)
                    : textModeShape(config),
              })
            }
            value={mode}
          >
            <option value="text">text</option>
            <option value="json">json</option>
          </select>
        </BasicPanelField>
        <BasicPanelField label="缺失变量">
          <select
            aria-label="缺失变量策略"
            className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
            onChange={(event) =>
              commit({ missing_variable: event.currentTarget.value })
            }
            value={
              ["error", "null", "empty"].includes(
                stringOrEmpty(config.missing_variable),
              )
                ? stringOrEmpty(config.missing_variable)
                : "error"
            }
          >
            <option value="error">error</option>
            <option value="null">null</option>
            <option value="empty">empty</option>
          </select>
        </BasicPanelField>
      </div>

      <section aria-label="Transform 输入变量" className="space-y-3">
        <h4 className="text-sm font-medium">输入变量与绑定</h4>
        {variables.length === 0 ? (
          <p className="text-muted-foreground text-xs">尚未声明输入变量。</p>
        ) : null}
        {variables.map((variable, index) => (
          <section
            aria-label={`输入变量 ${index + 1}`}
            className="border-border space-y-3 rounded-md border p-3"
            key={stringOrEmpty(variable.id) || `variable-${index}`}
          >
            <input type="hidden" value={stringOrEmpty(variable.id)} />
            <div className="flex items-center gap-2">
              <Input
                aria-label={`输入变量 ${index + 1} 名称`}
                onChange={(event) =>
                  replaceVariable(index, { name: event.currentTarget.value })
                }
                value={stringOrEmpty(variable.name)}
              />
              <Button
                onClick={() =>
                  commit({
                    input_variables: variables.filter(
                      (_, variableIndex) => variableIndex !== index,
                    ),
                  })
                }
                type="button"
                variant="ghost"
              >
                删除
              </Button>
            </div>
            <BasicValueTypeEditor
              onChange={(valueType) =>
                replaceVariable(index, { value_type: valueType })
              }
              value={variable.value_type}
            />
          </section>
        ))}
        <Button
          onClick={() => {
            const id = stableBasicRowId("variable");
            commit({
              input_variables: [
                ...variables,
                {
                  id,
                  name: id,
                  value_type: {
                    kind: "string",
                    collection: false,
                    nullable: false,
                  },
                },
              ],
            });
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          添加输入变量
        </Button>
      </section>

      <BasicNodeInputBindingsEditor
        items={variables
          .filter((variable) => typeof variable.id === "string")
          .map((variable) => ({
            id: variable.id as string,
            label: stringOrEmpty(variable.name) || (variable.id as string),
          }))}
        node={props.node}
        onCommand={(command) => dispatchBasicCommand(store, command)}
      />

      {mode === "text" ? (
        <BasicRestrictedTemplateEditor
          label="受限模板"
          onChange={(nextTemplate) =>
            commit({ template: nextTemplate, output_schema: null })
          }
          value={template}
        />
      ) : (
        <section aria-label="受限 JSON 模板" className="space-y-3">
          <h4 className="text-sm font-medium">受限 JSON 模板</h4>
          <BasicJsonEditor
            label="JSON 模板"
            onChange={(value) =>
              commit({
                template: {
                  version: 1,
                  template: value,
                  bindings: templateBindings,
                },
              })
            }
            value={Object.hasOwn(template, "template") ? template.template : {}}
          />
          {Object.entries(templateBindings).map(([id, binding]) => (
            <BasicBindingEditor
              key={id}
              label={`模板变量 ${id}`}
              onChange={(next) => {
                const bindings = { ...templateBindings };
                if (next === null) delete bindings[id];
                else bindings[id] = next;
                commit({
                  template: {
                    version: 1,
                    template: Object.hasOwn(template, "template")
                      ? template.template
                      : {},
                    bindings,
                  },
                });
              }}
              value={binding}
            />
          ))}
          <Button
            onClick={() => {
              const id = stableBasicRowId("binding");
              commit({
                template: {
                  version: 1,
                  template: Object.hasOwn(template, "template")
                    ? template.template
                    : {},
                  bindings: {
                    ...templateBindings,
                    [id]: { kind: "workflow_input", input_id: "" },
                  },
                },
              });
            }}
            size="sm"
            type="button"
            variant="outline"
          >
            添加模板变量
          </Button>
          <BasicJsonEditor
            label="JSON 输出 Schema"
            objectOnly
            onChange={(outputSchema) => commit({ output_schema: outputSchema })}
            value={recordOrEmpty(config.output_schema)}
          />
        </section>
      )}
    </BasicPanelShell>
  );
}
