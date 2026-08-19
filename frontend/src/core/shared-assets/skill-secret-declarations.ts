import { z } from "zod";

import {
  assetIdSchema,
  eligibleSkillCredentialSchema,
  skillCredentialBindingInputSchema,
  skillSecretDeclarationNameSchema,
  type SkillPublishAssetVersionInput,
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

export const skillPublishPlanRequirementSchema = z
  .object({
    name: skillSecretDeclarationNameSchema,
    optional: z.boolean(),
    suggested_credential_version_id: assetIdSchema.nullable(),
    eligible_credentials: z.array(eligibleSkillCredentialSchema),
  })
  .strict()
  .superRefine((value, context) => {
    const eligibleVersionIds = value.eligible_credentials.map(
      (credential) => credential.credential_version_id,
    );
    if (new Set(eligibleVersionIds).size !== eligibleVersionIds.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Eligible Credential versions must be unique",
        path: ["eligible_credentials"],
      });
    }
    if (
      value.suggested_credential_version_id !== null &&
      !eligibleVersionIds.includes(value.suggested_credential_version_id)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Suggested Credential version must be eligible",
        path: ["suggested_credential_version_id"],
      });
    }
  });

export const skillPublishPlanResponseSchema = z
  .object({
    skill_id: assetIdSchema,
    skill_version_id: assetIdSchema,
    asset_version: z.number().int().positive(),
    payload_checksum: sha256Schema,
    binding_revision: z.number().int().nonnegative(),
    secrets_autonomous: z.boolean(),
    requirements: z.array(skillPublishPlanRequirementSchema).max(256),
    request_id: z.string().min(1),
  })
  .strict()
  .refine(
    (value) =>
      new Set(value.requirements.map((requirement) => requirement.name))
        .size === value.requirements.length,
    {
      message: "Skill publish requirement names must be unique",
      path: ["requirements"],
    },
  );

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
export type SkillPublishPlanRequirement = z.infer<
  typeof skillPublishPlanRequirementSchema
>;
export type SkillPublishPlanResponse = z.infer<
  typeof skillPublishPlanResponseSchema
>;
export type SkillPublishSelections = Record<string, string>;

export async function sha256SkillContent(content: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(content),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export function initialSkillPublishSelections(
  plan: SkillPublishPlanResponse,
): SkillPublishSelections {
  return Object.fromEntries(
    plan.requirements.flatMap((requirement) =>
      requirement.suggested_credential_version_id === null
        ? []
        : [[requirement.name, requirement.suggested_credential_version_id]],
    ),
  );
}

export function mergeSkillPublishSelections(
  current: SkillPublishSelections,
  plan: SkillPublishPlanResponse,
  editedRequirementNames: ReadonlySet<string> = new Set(),
): SkillPublishSelections {
  return Object.fromEntries(
    plan.requirements.flatMap((requirement) => {
      const currentSelection = current[requirement.name];
      const stillEligible = requirement.eligible_credentials.some(
        (credential) => credential.credential_version_id === currentSelection,
      );
      const intentionallyUnbound =
        editedRequirementNames.has(requirement.name) && !currentSelection;
      const selected = intentionallyUnbound
        ? null
        : stillEligible
          ? currentSelection
          : requirement.suggested_credential_version_id;
      return selected ? [[requirement.name, selected]] : [];
    }),
  );
}

export function missingRequiredSkillPublishRequirements(
  plan: SkillPublishPlanResponse,
  selections: SkillPublishSelections,
): SkillPublishPlanRequirement[] {
  return plan.requirements.filter(
    (requirement) => !requirement.optional && !selections[requirement.name],
  );
}

export function skillPublishRequiredBindingsBlocked({
  plan,
  selections,
  skillActive,
  canApproveCredentials,
}: {
  plan: SkillPublishPlanResponse;
  selections: SkillPublishSelections;
  skillActive: boolean;
  canApproveCredentials: boolean;
}): boolean {
  if (!skillActive) return false;
  if (!canApproveCredentials) {
    return plan.requirements.some((requirement) => !requirement.optional);
  }
  return missingRequiredSkillPublishRequirements(plan, selections).length > 0;
}

export function buildSkillPublishInput({
  plan,
  selections,
  includeCredentialBindings,
  acknowledgeStaleBase = false,
}: {
  plan: SkillPublishPlanResponse;
  selections: SkillPublishSelections;
  includeCredentialBindings: boolean;
  acknowledgeStaleBase?: boolean;
}): SkillPublishAssetVersionInput {
  const bindings = plan.requirements.flatMap((requirement) => {
    const credentialVersionId = selections[requirement.name];
    if (!credentialVersionId) return [];
    return [
      skillCredentialBindingInputSchema.parse({
        name: requirement.name,
        credential_version_id: credentialVersionId,
      }),
    ];
  });
  return {
    expected_asset_version: plan.asset_version,
    expected_payload_checksum: plan.payload_checksum,
    expected_binding_revision: plan.binding_revision,
    acknowledge_stale_base: acknowledgeStaleBase,
    ...(includeCredentialBindings ? { credential_bindings: bindings } : {}),
  };
}
