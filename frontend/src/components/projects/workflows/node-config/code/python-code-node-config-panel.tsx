"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import {
  BasicNodeInputBindingsEditor,
  BasicValueTypeEditor,
} from "@/components/projects/workflows/node-config/basic/shared";
import type {
  WorkflowDraftNode,
  WorkflowNodeConfigPanelProps,
} from "@/components/projects/workflows/node-config/contracts";
import { useWorkflowWorkbenchFlushRegistry } from "@/components/projects/workflows/workbench/workbench-flush-context";
import { useWorkflowWorkbenchStore } from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { valueTypeFromJsonSchema } from "@/core/project-workflows/json-schema";
import {
  jsonSchemaSchema,
  pythonCodeNodeConfigV1Schema,
  type JsonSchema,
  type PythonCodeNodeConfigV1,
  type WorkflowValueType,
} from "@/core/project-workflows/types";

import {
  createPythonSourceController,
  utf8ByteLength,
} from "./python-source-controller";
import { WorkflowPythonEditor } from "./workflow-python-editor";

const DEFAULT_VALUE_TYPE: WorkflowValueType = Object.freeze({
  kind: "string",
  collection: false,
  nullable: false,
});

const DEFAULT_OUTPUT_SCHEMA: JsonSchema = Object.freeze({
  type: "object",
});

const PYTHON_IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/u;
const NODE_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

const safeConfig = (value: unknown): PythonCodeNodeConfigV1 => {
  const exact = pythonCodeNodeConfigV1Schema.safeParse(value);
  if (exact.success) return exact.data;
  const raw = isRecord(value) ? value : {};
  const inputVariables = Array.isArray(raw.input_variables)
    ? raw.input_variables.flatMap((candidate) => {
        const parsed = pythonCodeNodeConfigV1Schema.safeParse({
          source: "",
          input_variables: [candidate],
          output_schema: {},
          timeout_ms: null,
        });
        return parsed.success ? parsed.data.input_variables : [];
      })
    : [];
  const outputSchema = jsonSchemaSchema.safeParse(raw.output_schema);
  const timeout = raw.timeout_ms;
  return {
    source: typeof raw.source === "string" ? raw.source : "",
    input_variables: inputVariables,
    output_schema: outputSchema.success
      ? outputSchema.data
      : DEFAULT_OUTPUT_SCHEMA,
    timeout_ms:
      timeout === null ||
      (typeof timeout === "number" &&
        Number.isSafeInteger(timeout) &&
        timeout > 0)
        ? timeout
        : null,
  };
};

const strictConfig = (
  config: PythonCodeNodeConfigV1,
): PythonCodeNodeConfigV1 => {
  const parsed = pythonCodeNodeConfigV1Schema.parse(config);
  const ids = new Set(parsed.input_variables.map((variable) => variable.id));
  const names = new Set(
    parsed.input_variables.map((variable) => variable.name),
  );
  if (
    ids.size !== parsed.input_variables.length ||
    names.size !== parsed.input_variables.length
  ) {
    throw new Error("Python input variable stable IDs/names 必须唯一。");
  }
  valueTypeFromJsonSchema(parsed.output_schema, "object");
  return parsed;
};

export function buildPythonCodeConfigUpdate(
  node: WorkflowDraftNode,
  patch: Partial<PythonCodeNodeConfigV1>,
) {
  const config = strictConfig({ ...safeConfig(node.config), ...patch });
  return {
    type: "update_node_config" as const,
    node_id: typeof node.id === "string" ? node.id : "",
    config,
  };
}

export function appendPythonInputVariable(
  config: PythonCodeNodeConfigV1,
  identity: { id: string; name: string; value_type?: WorkflowValueType },
): PythonCodeNodeConfigV1 {
  if (
    !NODE_ID.test(identity.id) ||
    !PYTHON_IDENTIFIER.test(identity.name) ||
    config.input_variables.some(
      (variable) =>
        variable.id === identity.id || variable.name === identity.name,
    )
  ) {
    return config;
  }
  const candidate = pythonCodeNodeConfigV1Schema.safeParse({
    ...config,
    input_variables: [
      ...config.input_variables,
      {
        id: identity.id,
        name: identity.name,
        value_type: identity.value_type ?? DEFAULT_VALUE_TYPE,
      },
    ],
  });
  return candidate.success ? candidate.data : config;
}

export function movePythonInputVariable(
  config: PythonCodeNodeConfigV1,
  fromIndex: number,
  toIndex: number,
): PythonCodeNodeConfigV1 {
  if (
    fromIndex === toIndex ||
    fromIndex < 0 ||
    fromIndex >= config.input_variables.length ||
    toIndex < 0 ||
    toIndex >= config.input_variables.length
  ) {
    return config;
  }
  const inputVariables = [...config.input_variables];
  const [moved] = inputVariables.splice(fromIndex, 1);
  if (!moved) return config;
  inputVariables.splice(toIndex, 0, moved);
  return { ...config, input_variables: inputVariables };
}

export function removePythonInputVariable(
  config: PythonCodeNodeConfigV1,
  index: number,
): PythonCodeNodeConfigV1 {
  if (index < 0 || index >= config.input_variables.length) return config;
  return {
    ...config,
    input_variables: config.input_variables.filter(
      (_, variableIndex) => variableIndex !== index,
    ),
  };
}

export type PythonOutputSchemaParseResult =
  | Readonly<{ success: true; schema: JsonSchema }>
  | Readonly<{ success: false; issue: string }>;

export function parsePythonOutputSchema(
  text: string,
): PythonOutputSchemaParseResult {
  let value: unknown;
  try {
    value = JSON.parse(text) as unknown;
  } catch {
    return { success: false, issue: "输出 Schema 不是合法 JSON。" };
  }
  if (!isRecord(value)) {
    return { success: false, issue: "输出 Schema 必须是 JSON object。" };
  }
  const parsed = jsonSchemaSchema.safeParse(value);
  if (!parsed.success) {
    return { success: false, issue: "输出 Schema 不是 strict JSON object。" };
  }
  try {
    valueTypeFromJsonSchema(parsed.data, "object");
    return { success: true, schema: parsed.data };
  } catch {
    return {
      success: false,
      issue: "输出 Schema 必须是受支持的 non-null object JSON Schema。",
    };
  }
}

const newVariableId = (): string => {
  const generated = globalThis.crypto?.randomUUID?.();
  if (generated) return generated.toLowerCase();
  return "00000000-0000-4000-8000-000000000000";
};

const safeJsonText = (value: JsonSchema): string =>
  JSON.stringify(value, null, 2);

const safeDispatchIssue = (issues: readonly { message: string }[]): string =>
  issues[0]?.message ?? "Workflow Draft command 被拒绝。";

const pythonConfigPublicLimitIssue = (
  config: PythonCodeNodeConfigV1,
  maxSourceBytes: number,
  maxTimeoutMs: number,
): string | null => {
  const sourceBytes = utf8ByteLength(config.source);
  if (sourceBytes > maxSourceBytes) {
    return `Python source 为 ${sourceBytes} UTF-8 bytes，超过 Catalog 上限 ${maxSourceBytes} UTF-8 bytes。`;
  }
  if (config.timeout_ms !== null && config.timeout_ms > maxTimeoutMs) {
    return `timeout_ms 超过 Catalog 上限 ${maxTimeoutMs} ms。`;
  }
  return null;
};

const publicLimits = (props: WorkflowNodeConfigPanelProps) => {
  const source = props.catalogEntry.public_limits?.max_source_bytes;
  const timeout = props.catalogEntry.public_limits?.max_timeout_ms;
  return {
    maxSourceBytes:
      typeof source === "number" && Number.isSafeInteger(source) && source > 0
        ? source
        : null,
    maxTimeoutMs:
      typeof timeout === "number" &&
      Number.isSafeInteger(timeout) &&
      timeout > 0
        ? timeout
        : null,
  };
};

function lockReason(
  props: WorkflowNodeConfigPanelProps,
  hasLimits: boolean,
): string | null {
  if (props.readOnly) return "当前 Workflow 为只读状态。";
  if (props.disabled) return "当前节点已禁用，已保存配置保持不变。";
  if (props.catalogEntry.definition.type !== "python_code") {
    return "Catalog node identity 与 Python Code 不匹配，配置 fail closed。";
  }
  if (!props.capabilities.includes("workflow.edit")) {
    return "缺少 workflow.edit capability，配置保持只读。";
  }
  if (!props.capabilities.includes("workflow.code.use")) {
    return "缺少 workflow.code.use capability，配置保持只读。";
  }
  const grantedCapabilities = new Set<string>(props.capabilities);
  const missingCapability =
    props.catalogEntry.definition.required_capabilities.find(
      (capability) => !grantedCapabilities.has(capability),
    );
  if (missingCapability) {
    return `缺少 ${missingCapability} capability，配置保持只读。`;
  }
  if (props.catalogEntry.availability.state !== "enabled") {
    return `Code Sandbox 当前不可用：${props.catalogEntry.availability.reason_code}`;
  }
  if (!hasLimits) {
    return "Catalog 未提供 Code public limits，配置 fail closed。";
  }
  return null;
}

const configIssues = (
  raw: unknown,
  config: PythonCodeNodeConfigV1,
  maxSourceBytes: number | null,
  maxTimeoutMs: number | null,
): string[] => {
  const issues: string[] = [];
  if (config.source.length === 0) issues.push("source 尚未配置。");
  if (
    maxSourceBytes !== null &&
    utf8ByteLength(config.source) > maxSourceBytes
  ) {
    issues.push(`source 超过 ${maxSourceBytes} UTF-8 bytes。`);
  }
  const names = new Set<string>();
  const ids = new Set<string>();
  for (const [index, variable] of config.input_variables.entries()) {
    if (!PYTHON_IDENTIFIER.test(variable.name)) {
      issues.push(`输入变量 ${index + 1} 不是合法 Python identifier。`);
    }
    if (names.has(variable.name))
      issues.push(`输入变量名 ${variable.name} 重复。`);
    if (ids.has(variable.id))
      issues.push(`输入变量 stable ID ${variable.id} 重复。`);
    names.add(variable.name);
    ids.add(variable.id);
  }
  try {
    valueTypeFromJsonSchema(config.output_schema, "object");
  } catch {
    issues.push("output_schema 必须是受支持的 non-null object JSON Schema。");
  }
  if (
    config.timeout_ms !== null &&
    maxTimeoutMs !== null &&
    config.timeout_ms > maxTimeoutMs
  ) {
    issues.push(`timeout_ms 超过 Catalog 上限 ${maxTimeoutMs} ms。`);
  }
  if (!pythonCodeNodeConfigV1Schema.safeParse(raw).success) {
    issues.push(
      "Python Code Draft 尚未满足 strict config，可继续补齐标记字段。",
    );
  }
  return [...new Set(issues)];
};

export function PythonCodeNodeConfigPanel(props: WorkflowNodeConfigPanelProps) {
  const store = useWorkflowWorkbenchStore();
  const flushRegistry = useWorkflowWorkbenchFlushRegistry();
  const config = safeConfig(props.node.config);
  const limits = publicLimits(props);
  const reason = lockReason(
    props,
    limits.maxSourceBytes !== null && limits.maxTimeoutMs !== null,
  );
  const locked = reason !== null;
  const runtimeContractRef = useRef({
    writeEnabled: !locked,
    maxSourceBytes: limits.maxSourceBytes ?? 0,
    maxTimeoutMs: limits.maxTimeoutMs ?? 0,
  });
  runtimeContractRef.current = {
    writeEnabled: !locked,
    maxSourceBytes: limits.maxSourceBytes ?? 0,
    maxTimeoutMs: limits.maxTimeoutMs ?? 0,
  };
  const [panelIssue, setPanelIssue] = useState<string | null>(null);
  const [schemaDraft, setSchemaDraft] = useState(() => ({
    text: safeJsonText(config.output_schema),
    persisted: safeJsonText(config.output_schema),
  }));

  const sourceController = useMemo(
    () =>
      createPythonSourceController({
        flushKey: `workflow-python-source:${props.nodeId}`,
        initialSource: config.source,
        maxBytes: limits.maxSourceBytes ?? 0,
        registry: flushRegistry,
        commitSource: (source) => {
          const runtimeContract = runtimeContractRef.current;
          if (!runtimeContract.writeEnabled) {
            return {
              applied: false,
              safeMessage: "Python Code 当前不可编辑，source 仍保留在本地。",
            };
          }
          const currentNode = store
            .getState()
            .current.spec.nodes?.find((node) => node.id === props.nodeId);
          if (currentNode?.type !== "python_code") {
            return { applied: false, safeMessage: "Python Code 节点不存在。" };
          }
          try {
            const command = buildPythonCodeConfigUpdate(currentNode, {
              source,
            });
            const limitIssue = pythonConfigPublicLimitIssue(
              command.config,
              runtimeContract.maxSourceBytes,
              runtimeContract.maxTimeoutMs,
            );
            if (limitIssue) {
              return { applied: false, safeMessage: limitIssue };
            }
            const result = store.dispatch(command);
            if (result.applied) return { applied: true };
            const latest = store
              .getState()
              .current.spec.nodes?.find((node) => node.id === props.nodeId);
            const latestConfig = pythonCodeNodeConfigV1Schema.safeParse(
              latest?.config,
            );
            return latestConfig.success && latestConfig.data.source === source
              ? { applied: true }
              : {
                  applied: false,
                  safeMessage: safeDispatchIssue(result.issues),
                };
          } catch (error) {
            return {
              applied: false,
              safeMessage:
                error instanceof Error
                  ? error.message
                  : "Python source 无法写入 Workflow Draft。",
            };
          }
        },
      }),
    // One controller owns one node/Store/flush-registry generation. Catalog
    // limits and persisted source are synchronized below without replacing it.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- persisted source and limits synchronize without replacing a dirty generation
    [flushRegistry, props.nodeId, store],
  );
  const sourceState = useSyncExternalStore(
    sourceController.subscribe,
    sourceController.getState,
    sourceController.getState,
  );

  useEffect(() => {
    sourceController.receiveExternalSource(config.source);
  }, [config.source, sourceController]);
  useEffect(() => {
    sourceController.updateMaxBytes(limits.maxSourceBytes ?? 0);
  }, [limits.maxSourceBytes, sourceController]);
  useEffect(
    () => () => {
      sourceController.detach();
    },
    [sourceController],
  );
  const persistedOutputSchemaText = safeJsonText(config.output_schema);
  useEffect(() => {
    setSchemaDraft((current) => {
      if (current.persisted === persistedOutputSchemaText) return current;
      return current.text === current.persisted
        ? {
            text: persistedOutputSchemaText,
            persisted: persistedOutputSchemaText,
          }
        : { ...current, persisted: persistedOutputSchemaText };
    });
  }, [persistedOutputSchemaText]);

  const commitConfig = (patch: Partial<PythonCodeNodeConfigV1>) => {
    if (locked) return false;
    try {
      const currentNode =
        store
          .getState()
          .current.spec.nodes?.find((node) => node.id === props.nodeId) ??
        props.node;
      const command = buildPythonCodeConfigUpdate(currentNode, {
        ...patch,
        source: sourceState.buffer,
      });
      const limitIssue = pythonConfigPublicLimitIssue(
        command.config,
        limits.maxSourceBytes ?? 0,
        limits.maxTimeoutMs ?? 0,
      );
      if (limitIssue) {
        setPanelIssue(limitIssue);
        return false;
      }
      const result = store.dispatch(command);
      if (!result.applied) {
        setPanelIssue(safeDispatchIssue(result.issues));
        return false;
      }
      sourceController.acknowledgeCommitted(sourceState.buffer);
      setPanelIssue(null);
      return true;
    } catch (error) {
      setPanelIssue(
        error instanceof Error
          ? error.message
          : "Python Code 配置无法写入 Workflow Draft。",
      );
      return false;
    }
  };

  const issues = configIssues(
    props.node.config,
    { ...config, source: sourceState.buffer },
    limits.maxSourceBytes,
    limits.maxTimeoutMs,
  );
  if (panelIssue) issues.unshift(panelIssue);
  if (sourceState.issue) issues.unshift(sourceState.issue);

  return (
    <section aria-label="Python Code 配置" className="space-y-4 p-4">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold">代码执行</h3>
          <span className="rounded-full border px-2 py-1 text-xs font-medium">
            Python 3.12
          </span>
        </div>
        <p className="text-muted-foreground text-xs">
          固定 main(inputs) 合同；仅由 Worker 委派至 fresh 隔离 Sandbox。
        </p>
        {reason ? (
          <p className="text-destructive text-xs" role="status">
            {reason}
          </p>
        ) : (
          <p className="text-muted-foreground text-xs" role="status">
            Code Sandbox availability：Catalog enabled
          </p>
        )}
        <p className="text-muted-foreground text-xs">
          Catalog public limits：source ≤ {limits.maxSourceBytes ?? "不可用"}{" "}
          UTF-8 bytes；timeout ≤ {limits.maxTimeoutMs ?? "不可用"} ms。
        </p>
      </header>

      {issues.length > 0 ? (
        <ul
          aria-label="配置问题"
          className="border-destructive/40 bg-destructive/5 text-destructive space-y-1 rounded-md border p-3 text-xs"
          role="alert"
        >
          {[...new Set(issues)].map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      ) : null}

      <fieldset
        aria-disabled={locked}
        className="m-0 space-y-5 border-0 p-0"
        disabled={locked}
      >
        <legend className="sr-only">Python Code Draft settings</legend>

        <section aria-label="Python 输入变量" className="space-y-3">
          <div>
            <h4 className="text-sm font-medium">输入变量</h4>
            <p className="text-muted-foreground text-xs">
              ID 与顺序稳定；名称使用受限 Python identifier。
            </p>
          </div>
          {config.input_variables.length === 0 ? (
            <p className="text-muted-foreground text-xs">尚未声明输入变量。</p>
          ) : null}
          {config.input_variables.map((variable, index) => (
            <article
              aria-label={`Python 输入变量 ${index + 1}`}
              className="border-border space-y-3 rounded-md border p-3"
              key={variable.id}
            >
              <p className="text-muted-foreground text-[11px]">
                stable id: <code>{variable.id}</code>
              </p>
              <div className="flex flex-wrap gap-2">
                <Input
                  aria-label={`Python 输入变量 ${index + 1} 名称`}
                  defaultValue={variable.name}
                  onBlur={(event) => {
                    const name = event.currentTarget.value;
                    const next = config.input_variables.map(
                      (item, itemIndex) =>
                        itemIndex === index ? { ...item, name } : item,
                    );
                    commitConfig({ input_variables: next });
                  }}
                />
                <Button
                  aria-label={`上移 Python 输入变量 ${index + 1}`}
                  disabled={locked || index === 0}
                  onClick={() =>
                    commitConfig({
                      input_variables: movePythonInputVariable(
                        config,
                        index,
                        index - 1,
                      ).input_variables,
                    })
                  }
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  上移
                </Button>
                <Button
                  aria-label={`下移 Python 输入变量 ${index + 1}`}
                  disabled={
                    locked || index === config.input_variables.length - 1
                  }
                  onClick={() =>
                    commitConfig({
                      input_variables: movePythonInputVariable(
                        config,
                        index,
                        index + 1,
                      ).input_variables,
                    })
                  }
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  下移
                </Button>
                <Button
                  aria-label={`删除 Python 输入变量 ${index + 1}`}
                  disabled={locked}
                  onClick={() =>
                    commitConfig({
                      input_variables: removePythonInputVariable(config, index)
                        .input_variables,
                    })
                  }
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  删除
                </Button>
              </div>
              <BasicValueTypeEditor
                onChange={(valueType) =>
                  commitConfig({
                    input_variables: config.input_variables.map(
                      (item, itemIndex) =>
                        itemIndex === index
                          ? { ...item, value_type: valueType }
                          : item,
                    ),
                  })
                }
                value={variable.value_type}
              />
            </article>
          ))}
          <Button
            disabled={locked}
            onClick={() => {
              const next = appendPythonInputVariable(config, {
                id: newVariableId(),
                name: `input_${config.input_variables.length + 1}`,
              });
              commitConfig({ input_variables: next.input_variables });
            }}
            size="sm"
            type="button"
            variant="outline"
          >
            添加输入变量
          </Button>
        </section>

        <BasicNodeInputBindingsEditor
          items={config.input_variables.map((variable) => ({
            id: variable.id,
            label: variable.name,
          }))}
          node={props.node}
          onCommand={(command) => {
            if (locked) return;
            const result = store.dispatch(command);
            setPanelIssue(
              result.applied ? null : safeDispatchIssue(result.issues),
            );
          }}
        />

        <section aria-label="Python source" className="space-y-2">
          <h4 className="text-sm font-medium">source · main(inputs)</h4>
          <WorkflowPythonEditor
            disabled={locked}
            error={sourceState.issue}
            maxBytes={limits.maxSourceBytes ?? 0}
            onBlurCommit={() => {
              try {
                sourceController.commit();
              } catch {
                // The controller exposes a safe inline issue and leaves this
                // generation registered for the next Workbench flush.
              }
            }}
            onChange={sourceController.edit}
            onExplicitCommit={() => {
              try {
                sourceController.commit();
              } catch {
                // See onBlurCommit: failed generations intentionally remain.
              }
            }}
            readOnly={locked}
            value={sourceState.buffer}
          />
        </section>

        <label className="block space-y-1.5 text-sm">
          <span className="font-medium">输出 Schema</span>
          <Textarea
            aria-invalid={
              parsePythonOutputSchema(schemaDraft.text).success
                ? undefined
                : true
            }
            aria-label="Python 输出 Schema"
            onBlur={() => {
              const parsed = parsePythonOutputSchema(schemaDraft.text);
              if (!parsed.success) {
                setPanelIssue(parsed.issue);
                return;
              }
              if (commitConfig({ output_schema: parsed.schema })) {
                setSchemaDraft({
                  text: safeJsonText(parsed.schema),
                  persisted: safeJsonText(parsed.schema),
                });
              }
            }}
            onChange={(event) =>
              setSchemaDraft((current) => ({
                ...current,
                text: event.currentTarget.value,
              }))
            }
            spellCheck={false}
            value={schemaDraft.text}
          />
          <span className="text-muted-foreground block text-xs">
            仅接受 strict JSON object；main(inputs) 必须返回匹配对象。
          </span>
        </label>

        <label className="block space-y-1.5 text-sm">
          <span className="font-medium">timeout_ms</span>
          <Input
            aria-label="Python timeout_ms"
            defaultValue={config.timeout_ms ?? ""}
            max={limits.maxTimeoutMs ?? undefined}
            min={1}
            onBlur={(event) => {
              const text = event.currentTarget.value.trim();
              const timeout = text.length === 0 ? null : Number(text);
              if (
                timeout !== null &&
                (!Number.isSafeInteger(timeout) ||
                  timeout <= 0 ||
                  limits.maxTimeoutMs === null ||
                  timeout > limits.maxTimeoutMs)
              ) {
                setPanelIssue(
                  `timeout_ms 必须为 1–${limits.maxTimeoutMs ?? "不可用"} 的整数，留空使用冻结 policy。`,
                );
                return;
              }
              commitConfig({ timeout_ms: timeout });
            }}
            placeholder="使用冻结 policy"
            type="number"
          />
          <span className="text-muted-foreground block text-xs">
            留空使用 Run admission 冻结的 policy；节点值不得超过 Catalog public
            limit。
          </span>
        </label>
      </fieldset>
    </section>
  );
}
