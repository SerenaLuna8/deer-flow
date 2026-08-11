import { z } from "zod";

import { addUnicodeScalarIssues, codePointBoundedString } from "./validation";

const nonEmptyIdSchema = z.string().min(1);
const portTitleSchema = codePointBoundedString(1, 128);
export const workflowInputIdSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z][A-Za-z0-9_.:-]*$/);
export const workflowOutputIdSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z_][A-Za-z0-9_.:-]*$/);
export const workflowCredentialSlotIdSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z_][A-Za-z0-9_.:-]*$/);
export const workflowPortIdSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/);
export const edgeIdSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/);
const nodeIdSchema = z
  .string()
  .regex(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    "node identity must be a canonical lowercase UUID string",
  );
const positiveSafeIntegerSchema = z
  .number()
  .int()
  .positive()
  .refine(
    Number.isSafeInteger,
    "integer exceeds JavaScript safe-integer range",
  );
const nonnegativeSafeIntegerSchema = z
  .number()
  .int()
  .nonnegative()
  .refine(
    Number.isSafeInteger,
    "integer exceeds JavaScript safe-integer range",
  );

export const canonicalJsonNumberSchema = z
  .number()
  .finite()
  .refine(
    (value) => !Number.isInteger(value) || Number.isSafeInteger(value),
    "integer exceeds JavaScript safe-integer range",
  );

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export type JsonSchema = Record<string, JsonValue>;

export const jsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.null(),
    z.boolean(),
    canonicalJsonNumberSchema,
    z.string(),
    z.array(jsonValueSchema),
    z.record(z.string(), jsonValueSchema),
  ]),
);

export const jsonSchemaSchema: z.ZodType<JsonSchema> = z.record(
  z.string(),
  jsonValueSchema,
);

export const workflowNodeKindSchema = z.enum([
  "start",
  "llm",
  "condition",
  "transform",
  "variable_aggregate",
  "loop",
  "http_request",
  "python_code",
  "end",
]);

export const workflowValueTypeSchema = z
  .object({
    kind: z.enum(["string", "number", "boolean", "json", "messages"]),
    collection: z.boolean(),
    nullable: z.boolean(),
    schema_ref: nonEmptyIdSchema.optional(),
  })
  .strict();

export const valueBindingSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("literal"), value: jsonValueSchema }).strict(),
  z
    .object({
      kind: z.literal("workflow_input"),
      input_id: workflowInputIdSchema,
    })
    .strict(),
  z
    .object({
      kind: z.literal("loop_variable"),
      loop_node_id: nodeIdSchema,
      variable_id: nonEmptyIdSchema,
    })
    .strict(),
  z
    .object({
      kind: z.literal("node_output"),
      node_id: nodeIdSchema,
      output_id: workflowPortIdSchema,
      path: codePointBoundedString(0, 2048).optional(),
    })
    .strict(),
]);

export type ValueBinding = z.infer<typeof valueBindingSchema>;

export type PredicateClause = {
  left: ValueBinding;
  operator:
    | "eq"
    | "ne"
    | "gt"
    | "gte"
    | "lt"
    | "lte"
    | "contains"
    | "starts_with"
    | "ends_with"
    | "is_null"
    | "is_not_null";
  right?: ValueBinding;
};

export type PredicateAst = {
  op: "and" | "or";
  items: Array<PredicateAst | PredicateClause>;
};

export const predicateClauseSchema: z.ZodType<PredicateClause> = z
  .object({
    left: valueBindingSchema,
    operator: z.enum([
      "eq",
      "ne",
      "gt",
      "gte",
      "lt",
      "lte",
      "contains",
      "starts_with",
      "ends_with",
      "is_null",
      "is_not_null",
    ]),
    right: valueBindingSchema.optional(),
  })
  .strict();

export const predicateAstSchema: z.ZodType<PredicateAst> = z.lazy(() =>
  z
    .object({
      op: z.enum(["and", "or"]),
      items: z.array(z.union([predicateAstSchema, predicateClauseSchema])),
    })
    .strict(),
);

export const restrictedTemplateSchema = z
  .object({
    version: z.literal(1),
    segments: z.array(
      z.discriminatedUnion("kind", [
        z.object({ kind: z.literal("text"), value: z.string() }).strict(),
        z
          .object({ kind: z.literal("binding"), value: valueBindingSchema })
          .strict(),
      ]),
    ),
  })
  .strict();

export const restrictedJsonTemplateSchema = z
  .object({
    version: z.literal(1),
    template: jsonValueSchema,
    bindings: z.record(z.string(), valueBindingSchema),
  })
  .strict();

export const httpKeyValueBindingSchema = z
  .object({
    id: nonEmptyIdSchema,
    name: z.string().min(1),
    value: z.union([valueBindingSchema, restrictedTemplateSchema]),
  })
  .strict();

export const workflowNodeScopeSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("root") }).strict(),
  z
    .object({ kind: z.literal("loop_body"), loop_node_id: nodeIdSchema })
    .strict(),
]);

export const nodeExecutionPolicyV1Schema = z
  .object({
    retry: z.discriminatedUnion("mode", [
      z.object({ mode: z.literal("none") }).strict(),
      z
        .object({
          mode: z.literal("bounded"),
          max_attempts: positiveSafeIntegerSchema,
          backoff_ms: nonnegativeSafeIntegerSchema,
        })
        .strict(),
    ]),
    on_error: z.discriminatedUnion("mode", [
      z.object({ mode: z.literal("fail_workflow") }).strict(),
      z
        .object({
          mode: z.literal("route_error"),
          output_port_id: z.literal("error"),
        })
        .strict(),
      z
        .object({
          mode: z.literal("continue_with_typed_default"),
          value: jsonValueSchema,
        })
        .strict(),
    ]),
  })
  .strict();

const workflowNodeSpecBaseShape = {
  id: nodeIdSchema,
  type_version: z.literal(1),
  scope: workflowNodeScopeSchema,
  custom_label: z.string().nullable(),
  description: z.string().nullable(),
  input_bindings: z.record(z.string(), valueBindingSchema.nullable()),
  execution_policy: nodeExecutionPolicyV1Schema,
};

export const workflowNodeSpecBaseSchema = z
  .object(workflowNodeSpecBaseShape)
  .strict();

export const startNodeConfigV1Schema = z.object({}).strict();

export const llmNodeConfigV1Schema = z
  .object({
    model_ref: nonEmptyIdSchema,
    mode: z.enum(["chat", "completion"]),
    context_input_ids: z.array(nonEmptyIdSchema),
    messages: z.array(
      z
        .object({
          id: nonEmptyIdSchema,
          role: z.enum(["system", "user", "assistant"]),
          content: restrictedTemplateSchema,
        })
        .strict(),
    ),
    model_parameters: z.record(z.string(), jsonValueSchema),
    stream: z.boolean(),
    reasoning_output: z.enum(["omit", "provider_summary"]),
    structured_output: z
      .object({
        enabled: z.boolean(),
        schema: jsonSchemaSchema.nullable(),
      })
      .strict(),
  })
  .strict();

export const conditionNodeConfigV1Schema = z
  .object({
    branches: z
      .array(
        z
          .object({
            id: portTitleSchema,
            output_port_id: workflowPortIdSchema,
            label: portTitleSchema.nullable(),
            predicate: predicateAstSchema,
          })
          .strict(),
      )
      .min(1)
      .max(254),
    else_output_port_id: workflowPortIdSchema,
  })
  .strict()
  .superRefine((config, context) => {
    const outputPortIds = config.branches.map(
      (branch) => branch.output_port_id,
    );
    const seen = new Set<string>();
    outputPortIds.forEach((portId, index) => {
      if (seen.has(portId)) {
        context.addIssue({
          code: "custom",
          message: "Condition branch output port ids must be unique",
          path: ["branches", index, "output_port_id"],
        });
      }
      seen.add(portId);
    });
    if (seen.has(config.else_output_port_id)) {
      context.addIssue({
        code: "custom",
        message:
          "Condition ELSE output port id must be distinct from branch ports",
        path: ["else_output_port_id"],
      });
    }
    if (seen.has("error") || config.else_output_port_id === "error") {
      context.addIssue({
        code: "custom",
        message:
          "Condition derived output port conflicts with the fixed error port",
        path: ["else_output_port_id"],
      });
    }
  });

const transformInputVariableSchema = z
  .object({
    id: nonEmptyIdSchema,
    name: nonEmptyIdSchema,
    value_type: workflowValueTypeSchema,
  })
  .strict();

export const transformNodeConfigV1Schema = z.discriminatedUnion("mode", [
  z
    .object({
      mode: z.literal("text"),
      input_variables: z.array(transformInputVariableSchema),
      missing_variable: z.enum(["error", "null", "empty"]),
      template: restrictedTemplateSchema,
      output_schema: z.null(),
    })
    .strict(),
  z
    .object({
      mode: z.literal("json"),
      input_variables: z.array(transformInputVariableSchema),
      missing_variable: z.enum(["error", "null", "empty"]),
      template: restrictedJsonTemplateSchema,
      output_schema: jsonSchemaSchema,
    })
    .strict(),
]);

export const variableAggregateNodeConfigV1Schema = z
  .object({
    strategy: z.literal("exclusive_branch"),
    groups: z
      .array(
        z
          .object({
            id: workflowPortIdSchema,
            name: portTitleSchema,
            value_type: workflowValueTypeSchema,
            candidate_input_ids: z.array(nonEmptyIdSchema),
          })
          .strict(),
      )
      .min(1)
      .max(254),
  })
  .strict()
  .superRefine((config, context) => {
    const seen = new Set<string>();
    config.groups.forEach((group, index) => {
      if (seen.has(group.id)) {
        context.addIssue({
          code: "custom",
          message: "Variable Aggregate output port ids must be unique",
          path: ["groups", index, "id"],
        });
      }
      if (group.id === "next" || group.id === "error") {
        context.addIssue({
          code: "custom",
          message:
            "Variable Aggregate derived output port conflicts with a fixed port",
          path: ["groups", index, "id"],
        });
      }
      seen.add(group.id);
    });
  });

export const loopNodeConfigV1Schema = z
  .object({
    mode: z.literal("do_until"),
    body_entry_node_id: nodeIdSchema,
    body_exit_node_id: nodeIdSchema,
    max_iterations: positiveSafeIntegerSchema,
    termination_condition: predicateAstSchema,
    variables: z
      .array(
        z
          .object({
            id: nonEmptyIdSchema,
            name: portTitleSchema,
            value_type: workflowValueTypeSchema,
            initial_input_id: nonEmptyIdSchema,
            next_input_id: nonEmptyIdSchema,
            output_port_id: workflowPortIdSchema,
          })
          .strict(),
      )
      .min(1)
      .max(252),
  })
  .strict()
  .superRefine((config, context) => {
    const fixed = new Set(["body", "next", "error", "iteration_count"]);
    const seen = new Set<string>();
    config.variables.forEach((variable, index) => {
      if (seen.has(variable.output_port_id)) {
        context.addIssue({
          code: "custom",
          message: "Loop variable output port ids must be unique",
          path: ["variables", index, "output_port_id"],
        });
      }
      if (fixed.has(variable.output_port_id)) {
        context.addIssue({
          code: "custom",
          message: "Loop variable output port conflicts with a fixed port",
          path: ["variables", index, "output_port_id"],
        });
      }
      seen.add(variable.output_port_id);
    });
  });

export const httpRequestAuthV1Schema = z.discriminatedUnion("mode", [
  z.object({ mode: z.literal("none") }).strict(),
  z
    .object({
      mode: z.literal("endpoint_profile"),
      injection_profile_id: nonEmptyIdSchema,
      credential_slot_id: workflowCredentialSlotIdSchema,
    })
    .strict(),
]);

const httpRequestBodyV1Schema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("none") }).strict(),
  z
    .object({
      kind: z.literal("json"),
      template: restrictedJsonTemplateSchema,
    })
    .strict(),
  z
    .object({
      kind: z.literal("form_urlencoded"),
      fields: z.array(httpKeyValueBindingSchema),
    })
    .strict(),
  z
    .object({
      kind: z.literal("multipart_text"),
      fields: z.array(httpKeyValueBindingSchema),
    })
    .strict(),
  z
    .object({
      kind: z.literal("raw_text"),
      content_type: z.string().min(1),
      template: restrictedTemplateSchema,
    })
    .strict(),
]);

const httpStatusSchema = z
  .number()
  .int()
  .min(100)
  .max(599)
  .refine(
    Number.isSafeInteger,
    "integer exceeds JavaScript safe-integer range",
  );

const acceptedStatusRangeSchema = z
  .object({
    from: httpStatusSchema,
    to: httpStatusSchema,
  })
  .strict()
  .refine((value) => value.from <= value.to, {
    message: "accepted status range must be ordered",
    path: ["to"],
  });

export const httpRequestNodeConfigV1Schema = z
  .object({
    method: z.enum(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]),
    base_origin: z.string().min(1),
    path_template: restrictedTemplateSchema,
    query: z.array(httpKeyValueBindingSchema),
    headers: z.array(httpKeyValueBindingSchema),
    auth: httpRequestAuthV1Schema,
    body: httpRequestBodyV1Schema,
    timeout: z
      .object({
        connect_ms: nonnegativeSafeIntegerSchema.nullable(),
        read_ms: nonnegativeSafeIntegerSchema.nullable(),
        write_ms: nonnegativeSafeIntegerSchema.nullable(),
      })
      .strict(),
    response: z
      .object({
        mode: z.enum(["json", "text"]),
        accepted_statuses: z.array(acceptedStatusRangeSchema),
        schema: jsonSchemaSchema.nullable(),
      })
      .strict(),
  })
  .strict();

const pythonIdentifierSchema = z
  .string()
  .regex(/^[A-Za-z_][A-Za-z0-9_]*$/, "invalid Python identifier");

export const pythonCodeNodeConfigV1Schema = z
  .object({
    source: z.string(),
    input_variables: z.array(
      z
        .object({
          id: nodeIdSchema,
          name: pythonIdentifierSchema,
          value_type: workflowValueTypeSchema,
        })
        .strict(),
    ),
    output_schema: jsonSchemaSchema,
    timeout_ms: positiveSafeIntegerSchema.nullable(),
  })
  .strict();

export const endNodeConfigV1Schema = z.object({}).strict();

export const workflowNodeSpecSchema = z.discriminatedUnion("type", [
  z
    .object({
      ...workflowNodeSpecBaseShape,
      type: z.literal("start"),
      config: startNodeConfigV1Schema,
    })
    .strict(),
  z
    .object({
      ...workflowNodeSpecBaseShape,
      type: z.literal("llm"),
      config: llmNodeConfigV1Schema,
    })
    .strict(),
  z
    .object({
      ...workflowNodeSpecBaseShape,
      type: z.literal("condition"),
      config: conditionNodeConfigV1Schema,
    })
    .strict(),
  z
    .object({
      ...workflowNodeSpecBaseShape,
      type: z.literal("transform"),
      config: transformNodeConfigV1Schema,
    })
    .strict(),
  z
    .object({
      ...workflowNodeSpecBaseShape,
      type: z.literal("variable_aggregate"),
      config: variableAggregateNodeConfigV1Schema,
    })
    .strict(),
  z
    .object({
      ...workflowNodeSpecBaseShape,
      type: z.literal("loop"),
      config: loopNodeConfigV1Schema,
    })
    .strict(),
  z
    .object({
      ...workflowNodeSpecBaseShape,
      type: z.literal("http_request"),
      config: httpRequestNodeConfigV1Schema,
    })
    .strict(),
  z
    .object({
      ...workflowNodeSpecBaseShape,
      type: z.literal("python_code"),
      config: pythonCodeNodeConfigV1Schema,
    })
    .strict(),
  z
    .object({
      ...workflowNodeSpecBaseShape,
      type: z.literal("end"),
      config: endNodeConfigV1Schema,
    })
    .strict(),
]);

export const controlTransitionSchema = z
  .object({
    id: edgeIdSchema,
    source: z
      .object({ node_id: nodeIdSchema, port_id: workflowPortIdSchema })
      .strict(),
    target: z
      .object({ node_id: nodeIdSchema, port_id: workflowPortIdSchema })
      .strict(),
  })
  .strict();

export const workflowInputConstraintsV1Schema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("none") }).strict(),
  z
    .object({
      kind: z.literal("string"),
      min_length: nonnegativeSafeIntegerSchema.optional(),
      max_length: nonnegativeSafeIntegerSchema.optional(),
      pattern: z.string().optional(),
    })
    .strict(),
  z
    .object({
      kind: z.literal("number"),
      minimum: canonicalJsonNumberSchema.optional(),
      maximum: canonicalJsonNumberSchema.optional(),
    })
    .strict(),
  z
    .object({ kind: z.literal("enum"), options: z.array(jsonValueSchema) })
    .strict(),
]);

export const workflowInputDeclSchema = z
  .object({
    id: workflowInputIdSchema,
    name: portTitleSchema,
    label: portTitleSchema.nullable(),
    description: z.string().nullable(),
    value_type: workflowValueTypeSchema,
    required: z.boolean(),
    default: jsonValueSchema.optional(),
    constraints: workflowInputConstraintsV1Schema,
  })
  .strict();

export const workflowOutputDeclSchema = z
  .object({
    id: workflowOutputIdSchema,
    name: nonEmptyIdSchema,
    description: z.string().nullable(),
    value_type: workflowValueTypeSchema,
    source: valueBindingSchema.nullable(),
    default: jsonValueSchema.optional(),
  })
  .strict();

export const workflowCredentialSlotDeclSchema = z
  .object({
    id: workflowCredentialSlotIdSchema,
    name: nonEmptyIdSchema,
    purpose: z.literal("http_auth"),
    payload_schema: jsonSchemaSchema,
    required: z.literal(true),
  })
  .strict();

export const workflowSpecV1Schema = z
  .object({
    schema_version: z.literal(1),
    entry_node_id: nodeIdSchema,
    nodes: z.array(workflowNodeSpecSchema),
    transitions: z.array(controlTransitionSchema),
    workflow_inputs: z.array(workflowInputDeclSchema).max(255),
    workflow_outputs: z.array(workflowOutputDeclSchema),
    credential_slots: z.array(workflowCredentialSlotDeclSchema),
  })
  .strict()
  .superRefine((value, context) => {
    addUnicodeScalarIssues(value, context);
    const nodeIds = new Set<string>();
    value.nodes.forEach((node, index) => {
      if (nodeIds.has(node.id)) {
        context.addIssue({
          code: "custom",
          message: "Workflow node ids must be unique",
          path: ["nodes", index, "id"],
        });
      }
      nodeIds.add(node.id);
    });
    const workflowInputIds = new Set<string>();
    value.workflow_inputs.forEach((declaration, index) => {
      if (workflowInputIds.has(declaration.id)) {
        context.addIssue({
          code: "custom",
          message: "Workflow input ids must be unique",
          path: ["workflow_inputs", index, "id"],
        });
      }
      if (declaration.id === "next") {
        context.addIssue({
          code: "custom",
          message:
            "Workflow input port conflicts with the fixed Start next port",
          path: ["workflow_inputs", index, "id"],
        });
      }
      workflowInputIds.add(declaration.id);
    });
  });

export const canvasDocumentV1Schema = z
  .object({
    schema_version: z.literal(1),
    node_layouts: z.array(
      z
        .object({
          node_id: nodeIdSchema,
          position: z
            .object({
              x: canonicalJsonNumberSchema,
              y: canonicalJsonNumberSchema,
            })
            .strict(),
          parent_node_id: nodeIdSchema.optional(),
          collapsed: z.boolean().optional(),
        })
        .strict(),
    ),
    edge_layouts: z.array(
      z
        .object({
          edge_id: edgeIdSchema,
          routing: z.enum(["bezier", "smoothstep"]),
        })
        .strict(),
    ),
  })
  .strict()
  .superRefine((value, context) => addUnicodeScalarIssues(value, context));

export type WorkflowNodeKind = z.infer<typeof workflowNodeKindSchema>;
export type WorkflowValueType = z.infer<typeof workflowValueTypeSchema>;
export type WorkflowNodeScope = z.infer<typeof workflowNodeScopeSchema>;
export type WorkflowNodeSpecBase = z.infer<typeof workflowNodeSpecBaseSchema>;
export type NodeExecutionPolicyV1 = z.infer<typeof nodeExecutionPolicyV1Schema>;
export type RestrictedTemplate = z.infer<typeof restrictedTemplateSchema>;
export type RestrictedJsonTemplate = z.infer<
  typeof restrictedJsonTemplateSchema
>;
export type HttpKeyValueBinding = z.infer<typeof httpKeyValueBindingSchema>;
export type HttpRequestAuthV1 = z.infer<typeof httpRequestAuthV1Schema>;
export type ControlTransition = z.infer<typeof controlTransitionSchema>;
export type WorkflowInputConstraintsV1 = z.infer<
  typeof workflowInputConstraintsV1Schema
>;
export type WorkflowInputDecl = z.infer<typeof workflowInputDeclSchema>;
export type WorkflowOutputDecl = z.infer<typeof workflowOutputDeclSchema>;
export type WorkflowCredentialSlotDecl = z.infer<
  typeof workflowCredentialSlotDeclSchema
>;
export type WorkflowNodeSpec = z.infer<typeof workflowNodeSpecSchema>;
export type WorkflowSpecV1 = z.infer<typeof workflowSpecV1Schema>;
export type CanvasDocumentV1 = z.infer<typeof canvasDocumentV1Schema>;
export type StartNodeConfigV1 = z.infer<typeof startNodeConfigV1Schema>;
export type LlmNodeConfigV1 = z.infer<typeof llmNodeConfigV1Schema>;
export type ConditionNodeConfigV1 = z.infer<typeof conditionNodeConfigV1Schema>;
export type TransformNodeConfigV1 = z.infer<typeof transformNodeConfigV1Schema>;
export type VariableAggregateNodeConfigV1 = z.infer<
  typeof variableAggregateNodeConfigV1Schema
>;
export type LoopNodeConfigV1 = z.infer<typeof loopNodeConfigV1Schema>;
export type HttpRequestNodeConfigV1 = z.infer<
  typeof httpRequestNodeConfigV1Schema
>;
export type PythonCodeNodeConfigV1 = z.infer<
  typeof pythonCodeNodeConfigV1Schema
>;
export type EndNodeConfigV1 = z.infer<typeof endNodeConfigV1Schema>;

export type WorkflowNodeConfigByKind = {
  start: StartNodeConfigV1;
  llm: LlmNodeConfigV1;
  condition: ConditionNodeConfigV1;
  transform: TransformNodeConfigV1;
  variable_aggregate: VariableAggregateNodeConfigV1;
  loop: LoopNodeConfigV1;
  http_request: HttpRequestNodeConfigV1;
  python_code: PythonCodeNodeConfigV1;
  end: EndNodeConfigV1;
};
