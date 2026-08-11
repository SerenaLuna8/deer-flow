import { z } from "zod";

export const workflowModelParameterCapabilitySchema = z
  .object({
    name: z.enum(["temperature", "max_tokens"]),
    kind: z.enum(["number", "integer"]),
    minimum: z.number().finite(),
    maximum: z.number().finite(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.minimum >= value.maximum) {
      context.addIssue({
        code: "custom",
        path: ["maximum"],
        message: "Workflow Model parameter range is invalid",
      });
    }
    if (
      (value.name === "temperature" && value.kind !== "number") ||
      (value.name === "max_tokens" && value.kind !== "integer")
    ) {
      context.addIssue({
        code: "custom",
        path: ["kind"],
        message: "Workflow Model parameter kind is invalid",
      });
    }
    const expectedRange =
      value.name === "temperature" ? [-2, 2] : [1, 2_000_000];
    if (
      value.minimum !== expectedRange[0] ||
      value.maximum !== expectedRange[1]
    ) {
      context.addIssue({
        code: "custom",
        path: ["minimum"],
        message: "Workflow Model parameter range is invalid",
      });
    }
  });

export const workflowModelAuthoringCapabilitySchema = z
  .object({
    modes: z
      .array(z.enum(["chat", "completion"]))
      .min(1)
      .max(2),
    supports_streaming: z.boolean(),
    parameters: z.array(workflowModelParameterCapabilitySchema).max(2),
  })
  .strict()
  .superRefine((value, context) => {
    if (new Set(value.modes).size !== value.modes.length) {
      context.addIssue({
        code: "custom",
        path: ["modes"],
        message: "Workflow Model modes must be unique",
      });
    }
    const canonicalModes = ["chat", "completion"].filter((mode) =>
      value.modes.includes(mode as "chat" | "completion"),
    );
    if (value.modes.join("\0") !== canonicalModes.join("\0")) {
      context.addIssue({
        code: "custom",
        path: ["modes"],
        message: "Workflow Model modes must use canonical order",
      });
    }
    const names = value.parameters.map((parameter) => parameter.name);
    if (new Set(names).size !== names.length) {
      context.addIssue({
        code: "custom",
        path: ["parameters"],
        message: "Workflow Model parameters must be unique",
      });
    }
    const canonicalNames = ["temperature", "max_tokens"].filter((name) =>
      names.includes(name as "temperature" | "max_tokens"),
    );
    if (names.join("\0") !== canonicalNames.join("\0")) {
      context.addIssue({
        code: "custom",
        path: ["parameters"],
        message: "Workflow Model parameters must use canonical order",
      });
    }
  })
  .transform((value) =>
    Object.freeze({
      modes: Object.freeze([...value.modes]),
      supports_streaming: value.supports_streaming,
      parameters: Object.freeze(
        value.parameters.map((parameter) => Object.freeze({ ...parameter })),
      ),
    }),
  );

export const modelSchema = z
  .object({
    name: z.string().min(1),
    model: z.string().min(1),
    display_name: z.string().min(1),
    description: z.string(),
    supports_thinking: z.boolean(),
    supports_reasoning_effort: z.boolean(),
    supports_vision: z.boolean(),
    is_default: z.boolean(),
    workflow_authoring: workflowModelAuthoringCapabilitySchema,
  })
  .strict()
  .refine((model) => model.model === model.name, {
    path: ["model"],
    message: "Public model alias must equal its logical name",
  })
  .transform((model) => Object.freeze(model));

export const tokenUsageSettingsSchema = z
  .object({
    enabled: z.boolean(),
  })
  .strict();

export const modelsResponseSchema = z
  .object({
    models: z.array(modelSchema),
    token_usage: tokenUsageSettingsSchema,
  })
  .strict()
  .transform((response) =>
    Object.freeze({
      models: Object.freeze([...response.models]),
      token_usage: Object.freeze(response.token_usage),
    }),
  );

export type Model = z.infer<typeof modelSchema>;
export type WorkflowModelAuthoringCapability = z.infer<
  typeof workflowModelAuthoringCapabilitySchema
>;
export type WorkflowModelParameterCapability = z.infer<
  typeof workflowModelParameterCapabilitySchema
>;
export type TokenUsageSettings = z.infer<typeof tokenUsageSettingsSchema>;
export type ModelsResponse = z.infer<typeof modelsResponseSchema>;
