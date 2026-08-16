import { z } from "zod";

export const modelSchema = z
  .object({
    name: z.string().uuid(),
    model: z.string().uuid(),
    display_name: z.string().min(1),
    supports_thinking: z.boolean(),
    supports_reasoning_effort: z.boolean(),
    supports_vision: z.boolean(),
    // Legacy wire compatibility only. UI capability checks use supports_vision.
    supports_vision_bridge: z.boolean().default(false),
    is_default: z.boolean(),
  })
  .strict()
  .refine((model) => model.model === model.name, {
    path: ["model"],
    message: "Public model alias must equal its model ID",
  });

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
  .strict();

export type Model = z.infer<typeof modelSchema>;
export type TokenUsageSettings = z.infer<typeof tokenUsageSettingsSchema>;
export type ModelsResponse = z.infer<typeof modelsResponseSchema>;
