import { z } from "zod";

import {
  assetIdSchema,
  skillCredentialMappingStatusSchema,
  skillSecretDeclarationNameSchema,
  type SkillActivationInput,
} from "./types";

export const sha256Schema = z.string().regex(/^[a-f0-9]{64}$/u);

const skillFrontmatterSecretRequirementSchema = z
  .object({
    name: skillSecretDeclarationNameSchema,
    optional: z.boolean(),
  })
  .strict();

export const skillFrontmatterDiagnosticSchema = z
  .object({
    code: z.string().min(1),
    severity: z.enum(["error", "warning"]),
    field_path: z.array(z.union([z.string(), z.number().int().nonnegative()])),
    line: z.number().int().positive().nullable(),
    column: z.number().int().positive().nullable(),
    public_message: z.string().min(1),
  })
  .strict();

export const skillSecretProjectionSchema = z
  .object({
    required_secrets: z.array(skillFrontmatterSecretRequirementSchema).max(256),
    secrets_autonomous: z.boolean(),
    secrets_autonomous_explicit: z.boolean(),
    shorthand_count: z.number().int().nonnegative(),
  })
  .strict()
  .refine(
    (value) =>
      new Set(value.required_secrets.map((requirement) => requirement.name))
        .size === value.required_secrets.length,
    {
      message: "Skill secret requirement names must be unique",
      path: ["required_secrets"],
    },
  );

export const skillFrontmatterParseInputSchema = z
  .object({
    content: z.string().max(1024 * 1024),
    source_sha256: sha256Schema,
  })
  .strict();

export const skillFrontmatterParseResponseSchema = z
  .object({
    source_sha256: sha256Schema,
    valid: z.boolean(),
    patchable: z.boolean(),
    projection: skillSecretProjectionSchema.nullable(),
    diagnostics: z.array(skillFrontmatterDiagnosticSchema),
    request_id: z.string().min(1),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.valid !== (value.projection !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Valid Skill frontmatter requires a projection",
        path: ["projection"],
      });
    }
    if (!value.valid && value.patchable) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Invalid Skill frontmatter cannot be patched",
        path: ["patchable"],
      });
    }
  });

export const skillFrontmatterPatchInputSchema = z
  .object({
    content: z.string().max(1024 * 1024),
    source_sha256: sha256Schema,
    required_secrets: z.array(skillFrontmatterSecretRequirementSchema).max(256),
    secrets_autonomous: z.boolean(),
  })
  .strict()
  .refine(
    (value) =>
      new Set(value.required_secrets.map((requirement) => requirement.name))
        .size === value.required_secrets.length,
    {
      message: "Skill secret requirement names must be unique",
      path: ["required_secrets"],
    },
  );

export const skillFrontmatterPatchResponseSchema = z
  .object({
    source_sha256: sha256Schema,
    result_sha256: sha256Schema,
    content: z.string().max(1024 * 1024),
    changed: z.boolean(),
    changed_fields: z.array(z.enum(["required-secrets", "secrets-autonomous"])),
    projection: skillSecretProjectionSchema,
    diagnostics: z.array(skillFrontmatterDiagnosticSchema),
    request_id: z.string().min(1),
  })
  .strict()
  .refine(
    (value) =>
      new Set(value.changed_fields).size === value.changed_fields.length,
    {
      message: "Changed frontmatter fields must be unique",
      path: ["changed_fields"],
    },
  );

export const skillActivationRequirementSchema = z
  .object({
    name: skillSecretDeclarationNameSchema,
    optional: z.boolean(),
    mapping_status: skillCredentialMappingStatusSchema,
  })
  .strict();

export const skillActivationReadinessResponseSchema = z
  .object({
    skill_id: assetIdSchema,
    skill_version_id: assetIdSchema,
    revision: z.number().int().positive(),
    payload_checksum: sha256Schema,
    binding_revision: z.number().int().nonnegative(),
    secrets_autonomous: z.boolean(),
    ready: z.boolean(),
    required_count: z.number().int().nonnegative().max(256),
    configured_required_count: z.number().int().nonnegative().max(256),
    invalid_count: z.number().int().nonnegative().max(256),
    requirements: z.array(skillActivationRequirementSchema).max(256),
    request_id: z.string().min(1),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      new Set(value.requirements.map((requirement) => requirement.name))
        .size !== value.requirements.length
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill activation requirement names must be unique",
        path: ["requirements"],
      });
    }
    const required = value.requirements.filter(
      (requirement) => !requirement.optional,
    );
    const configuredRequired = required.filter(
      (requirement) => requirement.mapping_status === "configured",
    );
    const invalid = value.requirements.filter(
      (requirement) => requirement.mapping_status === "invalid",
    );
    if (value.required_count !== required.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill activation required count does not match requirements",
        path: ["required_count"],
      });
    }
    if (value.configured_required_count !== configuredRequired.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Skill activation configured required count does not match requirements",
        path: ["configured_required_count"],
      });
    }
    if (value.invalid_count !== invalid.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill activation invalid count does not match requirements",
        path: ["invalid_count"],
      });
    }
    const ready =
      configuredRequired.length === required.length && invalid.length === 0;
    if (value.ready !== ready) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill activation readiness does not match requirements",
        path: ["ready"],
      });
    }
  });

export type SkillFrontmatterDiagnostic = z.infer<
  typeof skillFrontmatterDiagnosticSchema
>;
export type SkillSecretProjection = z.infer<typeof skillSecretProjectionSchema>;
export type SkillFrontmatterParseInput = z.input<
  typeof skillFrontmatterParseInputSchema
>;
export type SkillFrontmatterParseResponse = z.infer<
  typeof skillFrontmatterParseResponseSchema
>;
export type SkillFrontmatterPatchInput = z.input<
  typeof skillFrontmatterPatchInputSchema
>;
export type SkillFrontmatterPatchResponse = z.infer<
  typeof skillFrontmatterPatchResponseSchema
>;
export type SkillActivationRequirement = z.infer<
  typeof skillActivationRequirementSchema
>;
export type SkillActivationReadinessResponse = z.infer<
  typeof skillActivationReadinessResponseSchema
>;

export async function sha256SkillContent(content: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(content),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export function missingRequiredSkillActivationRequirements(
  readiness: SkillActivationReadinessResponse,
): SkillActivationRequirement[] {
  return readiness.requirements.filter(
    (requirement) =>
      !requirement.optional && requirement.mapping_status !== "configured",
  );
}

export function skillActivationBlocked({
  readiness,
}: {
  readiness: SkillActivationReadinessResponse;
}): boolean {
  return !readiness.ready;
}

export function buildSkillActivationInput({
  readiness,
}: {
  readiness: SkillActivationReadinessResponse;
}): SkillActivationInput {
  return {
    expected_revision: readiness.revision,
    expected_payload_checksum: readiness.payload_checksum,
    expected_binding_revision: readiness.binding_revision,
  };
}
