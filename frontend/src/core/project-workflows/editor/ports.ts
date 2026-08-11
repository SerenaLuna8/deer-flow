import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import {
  workflowNodeRegistryV1,
  type PortDefinition,
} from "@/core/project-workflows/catalog";
import { valueTypeFromJsonSchema } from "@/core/project-workflows/json-schema";
import {
  workflowNodeKindSchema,
  workflowPortIdSchema,
  workflowValueTypeSchema,
  type JsonSchema,
  type WorkflowNodeKind,
  type WorkflowValueType,
} from "@/core/project-workflows/types";

export type WorkflowDraftPortLocale = "zh-CN" | "en-US";

export type WorkflowDraftPort = {
  id: string;
  label: string;
  titleI18n: { "zh-CN": string; "en-US": string };
  direction: "input" | "output";
  kind: "control" | "data";
  cardinality: "one" | "many";
  required: boolean;
  valueType: WorkflowValueType | null;
};

export type ResolvedDraftNodePorts = {
  inputPorts: WorkflowDraftPort[];
  outputPorts: WorkflowDraftPort[];
};

type JsonObject = Record<string, unknown>;

const definitionByType = new Map(
  workflowNodeRegistryV1.map((definition) => [definition.type, definition]),
);

const asObject = (value: unknown): JsonObject | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;

const asArray = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];

const nonEmptyString = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 ? value : null;

const safePortId = (value: unknown): string | null => {
  const parsed = workflowPortIdSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
};

const safeValueType = (value: unknown): WorkflowValueType | null => {
  const parsed = workflowValueTypeSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
};

const schemaValueType = (
  value: unknown,
  requirement: "any" | "object" = "any",
): WorkflowValueType | null => {
  const schema = asObject(value);
  if (schema === null) return null;
  try {
    return valueTypeFromJsonSchema(
      schema as unknown as JsonSchema,
      requirement,
    );
  } catch {
    return null;
  }
};

const localizedTitle = (
  zh: string,
  en: string = zh,
): WorkflowDraftPort["titleI18n"] => ({ "zh-CN": zh, "en-US": en });

const localizedLabel = (
  title: WorkflowDraftPort["titleI18n"],
  locale: WorkflowDraftPortLocale,
  fallback: string,
): string => title[locale] || title["zh-CN"] || title["en-US"] || fallback;

const fixedPort = (
  port: PortDefinition,
  direction: WorkflowDraftPort["direction"],
  locale: WorkflowDraftPortLocale,
): WorkflowDraftPort => ({
  id: port.id,
  label: localizedLabel(port.title_i18n, locale, port.id),
  titleI18n: port.title_i18n,
  direction,
  kind: port.kind,
  cardinality: port.cardinality,
  required: port.required,
  valueType: port.value_type,
});

const derivedPort = ({
  cardinality,
  en,
  id,
  kind,
  locale,
  valueType,
  zh,
}: {
  cardinality: WorkflowDraftPort["cardinality"];
  en?: string;
  id: string;
  kind: WorkflowDraftPort["kind"];
  locale: WorkflowDraftPortLocale;
  valueType: WorkflowValueType | null;
  zh: string;
}): WorkflowDraftPort => {
  const titleI18n = localizedTitle(zh, en);
  return {
    id,
    label: localizedLabel(titleI18n, locale, id),
    titleI18n,
    direction: "output",
    kind,
    cardinality,
    required: true,
    valueType,
  };
};

const dynamicDataPort = (
  id: string,
  zh: string,
  en: string | undefined,
  locale: WorkflowDraftPortLocale,
  valueType: WorkflowValueType | null,
): WorkflowDraftPort =>
  derivedPort({
    cardinality: "many",
    en,
    id,
    kind: "data",
    locale,
    valueType,
    zh,
  });

const dynamicControlPort = (
  id: string,
  zh: string,
  en: string | undefined,
  locale: WorkflowDraftPortLocale,
): WorkflowDraftPort =>
  derivedPort({
    cardinality: "one",
    en,
    id,
    kind: "control",
    locale,
    valueType: null,
    zh,
  });

function derivedOutputPorts(
  document: WorkflowPersistedDocumentV1,
  nodeType: WorkflowNodeKind,
  config: JsonObject,
  locale: WorkflowDraftPortLocale,
): WorkflowDraftPort[] {
  switch (nodeType) {
    case "start":
      return asArray(document.spec.workflow_inputs).flatMap((candidate) => {
        const declaration = asObject(candidate);
        const id = safePortId(declaration?.id);
        if (id === null) return [];
        const label =
          nonEmptyString(declaration?.label) ??
          nonEmptyString(declaration?.name) ??
          id;
        return [
          dynamicDataPort(
            id,
            label,
            label,
            locale,
            safeValueType(declaration?.value_type),
          ),
        ];
      });
    case "condition": {
      const ports = asArray(config.branches).flatMap((branch) => {
        const value = asObject(branch);
        const id = safePortId(value?.output_port_id);
        if (id === null) return [];
        const label =
          nonEmptyString(value?.label) ?? nonEmptyString(value?.id) ?? id;
        return [dynamicControlPort(id, label, label, locale)];
      });
      const fallback = safePortId(config.else_output_port_id);
      if (fallback !== null) {
        ports.push(dynamicControlPort(fallback, "否则", "ELSE", locale));
      }
      return ports;
    }
    case "llm": {
      const structured = asObject(config.structured_output);
      const valueType: WorkflowValueType | null =
        structured?.enabled === true
          ? schemaValueType(structured.schema, "object")
          : structured?.enabled === false
            ? { kind: "json", collection: false, nullable: true }
            : null;
      return [
        dynamicDataPort(
          "result",
          "结构化结果",
          "Structured Result",
          locale,
          valueType,
        ),
      ];
    }
    case "transform": {
      const valueType: WorkflowValueType | null =
        config.mode === "text"
          ? { kind: "string", collection: false, nullable: false }
          : config.mode === "json"
            ? schemaValueType(config.output_schema)
            : null;
      return [
        dynamicDataPort(
          "result",
          "转换结果",
          "Transform Result",
          locale,
          valueType,
        ),
      ];
    }
    case "variable_aggregate":
      return asArray(config.groups).flatMap((group) => {
        const value = asObject(group);
        const id = safePortId(value?.id);
        if (id === null) return [];
        const label = nonEmptyString(value?.name) ?? id;
        return [
          dynamicDataPort(
            id,
            label,
            label,
            locale,
            safeValueType(value?.value_type),
          ),
        ];
      });
    case "loop":
      return asArray(config.variables).flatMap((variable) => {
        const value = asObject(variable);
        const id = safePortId(value?.output_port_id);
        if (id === null) return [];
        const label = nonEmptyString(value?.name) ?? id;
        return [
          dynamicDataPort(
            id,
            label,
            label,
            locale,
            safeValueType(value?.value_type),
          ),
        ];
      });
    case "http_request": {
      const response = asObject(config.response);
      const valueType: WorkflowValueType | null =
        response?.mode === "text"
          ? { kind: "string", collection: false, nullable: false }
          : response?.mode === "json"
            ? schemaValueType(response.schema)
            : null;
      return [
        dynamicDataPort("body", "响应体", "Response Body", locale, valueType),
      ];
    }
    case "python_code":
      return [
        dynamicDataPort(
          "result",
          "执行结果",
          "Execution Result",
          locale,
          schemaValueType(config.output_schema, "object"),
        ),
      ];
    case "end":
      return [];
  }
}

const appendUniquePorts = (
  fixed: WorkflowDraftPort[],
  dynamic: WorkflowDraftPort[],
): WorkflowDraftPort[] => {
  const result = [...fixed];
  const ids = new Set(fixed.map((port) => port.id));
  for (const port of dynamic) {
    if (ids.has(port.id)) continue;
    ids.add(port.id);
    result.push(port);
  }
  return result;
};

/**
 * Resolve the one partial-safe port projection shared by Canvas and Inspector.
 * Publish-grade validation remains the strict Catalog/Compiler authority.
 */
export function resolveDraftNodePorts(
  document: WorkflowPersistedDocumentV1,
  nodeId: string,
  locale: WorkflowDraftPortLocale,
): ResolvedDraftNodePorts {
  const node = asArray(document.spec.nodes)
    .map(asObject)
    .find((candidate) => candidate?.id === nodeId);
  const parsedKind = workflowNodeKindSchema.safeParse(node?.type);
  if (!node || !parsedKind.success) {
    return { inputPorts: [], outputPorts: [] };
  }
  const definition = definitionByType.get(parsedKind.data);
  if (definition === undefined) {
    return { inputPorts: [], outputPorts: [] };
  }
  const inputPorts = definition.input_ports.map((port) =>
    fixedPort(port, "input", locale),
  );
  const fixedOutputs = definition.output_ports.map((port) =>
    fixedPort(port, "output", locale),
  );
  const config = asObject(node.config) ?? {};
  return {
    inputPorts,
    outputPorts: appendUniquePorts(
      fixedOutputs,
      derivedOutputPorts(document, parsedKind.data, config, locale),
    ),
  };
}

export function workflowDraftPortSignature(
  ports: ResolvedDraftNodePorts,
): string {
  const signature = (value: readonly WorkflowDraftPort[]) =>
    value.map((port) => [port.id, port.direction, port.kind, port.cardinality]);
  return JSON.stringify([
    signature(ports.inputPorts),
    signature(ports.outputPorts),
  ]);
}
