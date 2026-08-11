"use client";

import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import { useWorkflowWorkbenchStore } from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { llmNodeConfigV1Schema } from "@/core/project-workflows/types";

import {
  BasicExecutionPolicyEditor,
  BasicJsonEditor,
  BasicNodeInputBindingsEditor,
  BasicPanelField,
  BasicPanelShell,
  BasicRestrictedTemplateEditor,
  basicPanelWriteDisabled,
  dispatchBasicCommand,
  isRecord,
  nodeConfigOrEmpty,
  recordOrEmpty,
  safeJsonText,
  stableBasicRowId,
  stringOrEmpty,
  type BasicDraftNode,
} from "./shared";

const LLM_CONFIG_FIELDS = new Set([
  "model_ref",
  "mode",
  "context_input_ids",
  "messages",
  "model_parameters",
  "stream",
  "reasoning_output",
  "structured_output",
]);

export function buildLlmNodeConfigUpdate(
  node: BasicDraftNode,
  patch: Record<string, unknown>,
) {
  const current = nodeConfigOrEmpty(node);
  const config = Object.fromEntries(
    Object.entries(current).filter(([field]) => LLM_CONFIG_FIELDS.has(field)),
  );
  for (const [field, value] of Object.entries(patch)) {
    if (LLM_CONFIG_FIELDS.has(field)) config[field] = structuredClone(value);
  }
  return {
    type: "update_node_config" as const,
    node_id: stringOrEmpty(node.id),
    config,
  };
}

const llmMessages = (config: Record<string, unknown>) =>
  Array.isArray(config.messages) ? config.messages.filter(isRecord) : [];

const contextInputIds = (config: Record<string, unknown>): string[] =>
  Array.isArray(config.context_input_ids)
    ? config.context_input_ids.filter(
        (value): value is string => typeof value === "string",
      )
    : [];

function numericParameterValue(
  parameters: Record<string, unknown>,
  name: string,
): number | "" {
  const value = parameters[name];
  return typeof value === "number" ? value : "";
}

function llmIssues(config: Record<string, unknown>): string[] {
  const issues: string[] = [];
  if (typeof config.model_ref !== "string" || config.model_ref.length === 0) {
    issues.push("模型引用未绑定。");
  }
  const mode = config.mode;
  if (mode !== "chat" && mode !== "completion") {
    issues.push("请选择 chat 或 completion 模式。");
  }
  if (llmMessages(config).length === 0) {
    issues.push(
      mode === "completion"
        ? "Completion 单模板尚未配置。"
        : "受限消息模板尚未配置。",
    );
  }
  const structured = recordOrEmpty(config.structured_output);
  if (structured.enabled === true && !isRecord(structured.schema)) {
    issues.push("结构化输出已开启，但 JSON Schema 未配置。");
  }
  if (Object.keys(config).some((field) => !LLM_CONFIG_FIELDS.has(field))) {
    issues.push("LLM 配置包含不支持的字段；这些字段不会显示或写回。");
  }
  if (!llmNodeConfigV1Schema.safeParse(config).success) {
    issues.push("LLM Draft 尚未满足发布合同，可继续补齐标记字段。");
  }
  return [...new Set(issues)];
}

function modelCatalogIssues(props: WorkflowNodeConfigPanelProps): string[] {
  const catalog = props.modelCatalog;
  if (catalog?.status !== "ready") {
    return ["模型目录当前不可用，逻辑模型引用保持只读。"];
  }
  const modelRef = stringOrEmpty(nodeConfigOrEmpty(props.node).model_ref);
  if (
    modelRef.length > 0 &&
    !catalog.models.some((model) => model.name === modelRef)
  ) {
    return ["当前逻辑模型引用不在可用目录中，请重新选择后再发布。"];
  }
  const selected = catalog.models.find((model) => model.name === modelRef);
  const config = nodeConfigOrEmpty(props.node);
  const mode = config.mode;
  if (
    selected !== undefined &&
    (mode === "chat" || mode === "completion") &&
    !selected.workflow_authoring.modes.includes(mode)
  ) {
    return ["所选逻辑模型不支持当前调用模式，请重新选择模式后再发布。"];
  }
  if (
    selected !== undefined &&
    config.stream === true &&
    !selected.workflow_authoring.supports_streaming
  ) {
    return ["所选逻辑模型未声明流式能力，不能启用流式输出。"];
  }
  const parameters = recordOrEmpty(config.model_parameters);
  const declaredParameters = new Map(
    selected?.workflow_authoring.parameters.map((parameter) => [
      parameter.name,
      parameter,
    ]),
  );
  for (const [name, value] of Object.entries(parameters)) {
    const capability = declaredParameters.get(
      name as "temperature" | "max_tokens",
    );
    if (capability === undefined) {
      return ["模型参数包含服务端未声明的字段，请移除后再发布。"];
    }
    if (
      typeof value !== "number" ||
      !Number.isFinite(value) ||
      value < capability.minimum ||
      value > capability.maximum ||
      (capability.kind === "integer" && !Number.isInteger(value))
    ) {
      return ["模型参数超出服务端声明的类型或范围。"];
    }
  }
  if (
    config.reasoning_output === "provider_summary" &&
    selected !== undefined &&
    !selected.supports_thinking &&
    !selected.supports_reasoning_effort
  ) {
    return ["所选逻辑模型未声明推理能力，不能输出 Provider 摘要。"];
  }
  return [];
}

export function LlmNodeConfigPanel(props: WorkflowNodeConfigPanelProps) {
  const store = useWorkflowWorkbenchStore();
  const config = nodeConfigOrEmpty(props.node);
  const messages = llmMessages(config);
  const contextIds = contextInputIds(config);
  const parameters = recordOrEmpty(config.model_parameters);
  const structured = recordOrEmpty(config.structured_output);
  const mode = config.mode === "completion" ? "completion" : "chat";
  const locked = basicPanelWriteDisabled(props);
  const modelCatalog = props.modelCatalog;
  const models = modelCatalog?.status === "ready" ? modelCatalog.models : [];
  const modelRef = stringOrEmpty(config.model_ref);
  const modelRefKnown = models.some((model) => model.name === modelRef);
  const selectedModel = models.find((model) => model.name === modelRef);
  const supportedModes = selectedModel?.workflow_authoring.modes ?? [];
  const parameterCapabilities =
    selectedModel?.workflow_authoring.parameters ?? [];
  const reasoningSummaryAvailable =
    selectedModel?.supports_thinking === true ||
    selectedModel?.supports_reasoning_effort === true;
  const commit = (patch: Record<string, unknown>) =>
    dispatchBasicCommand(store, buildLlmNodeConfigUpdate(props.node, patch));
  const replaceMessage = (index: number, patch: Record<string, unknown>) =>
    commit({
      messages: messages.map((message, messageIndex) =>
        messageIndex === index ? { ...message, ...patch } : message,
      ),
    });
  const selectModel = (nextModelRef: string) => {
    const nextModel = models.find((model) => model.name === nextModelRef);
    if (nextModel === undefined) return;
    const nextModes = nextModel.workflow_authoring.modes;
    const nextMode = nextModes.includes(mode) ? mode : nextModes[0];
    if (nextMode === undefined) return;
    const allowedParameters = new Set(
      nextModel.workflow_authoring.parameters.map(
        (parameter) => parameter.name,
      ),
    );
    commit({
      model_ref: nextModelRef,
      mode: nextMode,
      model_parameters: Object.fromEntries(
        Object.entries(parameters).filter(([name]) =>
          allowedParameters.has(name as "temperature" | "max_tokens"),
        ),
      ),
      stream:
        nextModel.workflow_authoring.supports_streaming &&
        config.stream === true,
      reasoning_output:
        (nextModel.supports_thinking || nextModel.supports_reasoning_effort) &&
        config.reasoning_output === "provider_summary"
          ? "provider_summary"
          : "omit",
    });
  };

  return (
    <BasicPanelShell
      disabled={locked && !props.readOnly}
      issues={[...llmIssues(config), ...modelCatalogIssues(props)]}
      readOnly={props.readOnly}
      title="大模型配置"
    >
      <div className="border-border bg-muted/30 rounded-md border p-3 text-xs">
        固定无工具调用；不接入 Agent memory、知识检索、文件、Credential 或 raw
        chain-of-thought。
      </div>
      <BasicPanelField
        help="只保存逻辑 Model ref；模型能力与 Credential 由服务端重新授权。"
        label="逻辑模型引用"
      >
        <select
          aria-label="逻辑模型引用"
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          disabled={modelCatalog?.status !== "ready"}
          onChange={(event) => selectModel(event.currentTarget.value)}
          value={modelRef}
        >
          {modelRef.length === 0 ? (
            <option value="">请选择逻辑模型</option>
          ) : null}
          {modelRef.length > 0 && !modelRefKnown ? (
            <option value={modelRef}>{modelRef}（当前不可用）</option>
          ) : null}
          {models.map((model) => (
            <option key={model.name} value={model.name}>
              {model.display_name}（{model.name}）
            </option>
          ))}
        </select>
      </BasicPanelField>
      <BasicPanelField label="调用模式">
        <select
          aria-label="LLM 调用模式"
          className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
          disabled={selectedModel === undefined}
          onChange={(event) =>
            commit({
              mode: event.currentTarget.value,
              ...(event.currentTarget.value === "completion"
                ? { messages: messages.slice(0, 1) }
                : {}),
            })
          }
          value={mode}
        >
          {!supportedModes.includes(mode) ? (
            <option value={mode}>{mode}（当前模型不支持）</option>
          ) : null}
          {supportedModes.map((supportedMode) => (
            <option key={supportedMode} value={supportedMode}>
              {supportedMode}
            </option>
          ))}
        </select>
      </BasicPanelField>

      <section aria-label="上下文输入" className="space-y-2">
        <h4 className="text-sm font-medium">上下文输入 ID（有序）</h4>
        {contextIds.map((id, index) => (
          <div className="flex gap-2" key={`${id}-${index}`}>
            <Input
              aria-label={`上下文输入 ${index + 1}`}
              onChange={(event) =>
                commit({
                  context_input_ids: contextIds.map((item, itemIndex) =>
                    itemIndex === index ? event.currentTarget.value : item,
                  ),
                })
              }
              value={id}
            />
            <Button
              onClick={() =>
                commit({
                  context_input_ids: contextIds.filter(
                    (_, itemIndex) => itemIndex !== index,
                  ),
                })
              }
              type="button"
              variant="ghost"
            >
              删除
            </Button>
          </div>
        ))}
        <Button
          onClick={() =>
            commit({
              context_input_ids: [...contextIds, stableBasicRowId("context")],
            })
          }
          size="sm"
          type="button"
          variant="outline"
        >
          添加上下文输入
        </Button>
      </section>

      <section aria-label="受限消息模板" className="space-y-3">
        <h4 className="text-sm font-medium">
          {mode === "completion" ? "Completion 单模板" : "受限消息模板"}
        </h4>
        {messages.length === 0 ? (
          <p className="text-muted-foreground text-xs">尚未添加消息。</p>
        ) : null}
        {messages.map((message, index) => (
          <section
            aria-label={`消息 ${index + 1}`}
            className="border-border space-y-3 rounded-md border p-3"
            key={stringOrEmpty(message.id) || `message-${index}`}
          >
            <input type="hidden" value={stringOrEmpty(message.id)} />
            {mode === "chat" ? (
              <BasicPanelField label="角色">
                <select
                  aria-label={`消息 ${index + 1} 角色`}
                  className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
                  onChange={(event) =>
                    replaceMessage(index, { role: event.currentTarget.value })
                  }
                  value={
                    ["system", "user", "assistant"].includes(
                      stringOrEmpty(message.role),
                    )
                      ? stringOrEmpty(message.role)
                      : "user"
                  }
                >
                  <option value="system">system</option>
                  <option value="user">user</option>
                  <option value="assistant">assistant</option>
                </select>
              </BasicPanelField>
            ) : null}
            <BasicRestrictedTemplateEditor
              label={mode === "completion" ? "受限单模板" : "消息内容"}
              onChange={(content) => replaceMessage(index, { content })}
              value={message.content}
            />
            <Button
              onClick={() =>
                commit({
                  messages: messages.filter(
                    (_, messageIndex) => messageIndex !== index,
                  ),
                })
              }
              size="sm"
              type="button"
              variant="ghost"
            >
              删除消息
            </Button>
          </section>
        ))}
        <Button
          disabled={mode === "completion" && messages.length >= 1}
          onClick={() =>
            commit({
              messages: [
                ...messages,
                {
                  id: stableBasicRowId("message"),
                  role: "user",
                  content: { version: 1, segments: [] },
                },
              ],
            })
          }
          size="sm"
          type="button"
          variant="outline"
        >
          {mode === "completion" ? "添加单模板" : "添加消息"}
        </Button>
      </section>

      <BasicNodeInputBindingsEditor
        items={contextIds.map((id) => ({ id, label: id }))}
        node={props.node}
        onCommand={(command) => dispatchBasicCommand(store, command)}
      />

      <section aria-label="通用模型参数" className="space-y-3">
        <h4 className="text-sm font-medium">受限通用参数子集</h4>
        {selectedModel === undefined ? (
          <p className="text-muted-foreground text-xs">
            选择可用逻辑模型后，才会显示服务端声明的参数。
          </p>
        ) : parameterCapabilities.length === 0 ? (
          <p className="text-muted-foreground text-xs">
            所选模型未声明可编辑的通用参数。
          </p>
        ) : null}
        <div className="grid gap-3 sm:grid-cols-3">
          {parameterCapabilities.map((capability) => (
            <BasicPanelField key={capability.name} label={capability.name}>
              <Input
                aria-label={capability.name}
                max={capability.maximum}
                min={capability.minimum}
                onChange={(event) => {
                  const next = { ...parameters };
                  if (event.currentTarget.value === "") {
                    delete next[capability.name];
                  } else {
                    next[capability.name] = Number(event.currentTarget.value);
                  }
                  commit({ model_parameters: next });
                }}
                step={capability.kind === "integer" ? 1 : "any"}
                type="number"
                value={numericParameterValue(parameters, capability.name)}
              />
            </BasicPanelField>
          ))}
        </div>
      </section>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex items-center gap-2 text-sm">
          <input
            checked={config.stream === true}
            disabled={
              selectedModel?.workflow_authoring.supports_streaming !== true
            }
            onChange={(event) =>
              commit({ stream: event.currentTarget.checked })
            }
            type="checkbox"
          />
          流式输出
        </label>
        <BasicPanelField label="推理输出">
          <select
            aria-label="推理输出"
            className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
            disabled={selectedModel === undefined}
            onChange={(event) =>
              commit({ reasoning_output: event.currentTarget.value })
            }
            value={
              config.reasoning_output === "provider_summary"
                ? "provider_summary"
                : "omit"
            }
          >
            <option value="omit">不输出</option>
            <option
              disabled={!reasoningSummaryAvailable}
              value="provider_summary"
            >
              Provider 摘要
            </option>
          </select>
        </BasicPanelField>
      </div>

      <section aria-label="结构化输出" className="space-y-3">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            checked={structured.enabled === true}
            onChange={(event) =>
              commit({
                structured_output: {
                  enabled: event.currentTarget.checked,
                  schema: isRecord(structured.schema)
                    ? structured.schema
                    : null,
                },
              })
            }
            type="checkbox"
          />
          启用结构化输出
        </label>
        {structured.enabled === true ? (
          <BasicJsonEditor
            label="结构化输出 JSON Schema"
            objectOnly
            onChange={(schema) =>
              commit({
                structured_output: { enabled: true, schema },
              })
            }
            value={isRecord(structured.schema) ? structured.schema : {}}
          />
        ) : null}
        <p className="text-muted-foreground text-xs">
          当前 Schema：{safeJsonText(structured.schema, "null")}
        </p>
      </section>

      <BasicExecutionPolicyEditor
        node={props.node}
        onCommand={(command) => dispatchBasicCommand(store, command)}
      />
    </BasicPanelShell>
  );
}
