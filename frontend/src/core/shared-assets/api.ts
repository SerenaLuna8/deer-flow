import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  skillFrontmatterParseInputSchema,
  skillFrontmatterParseResponseSchema,
  skillFrontmatterDiagnosticSchema,
  skillFrontmatterPatchInputSchema,
  skillFrontmatterPatchResponseSchema,
  skillActivationReadinessResponseSchema,
  type SkillFrontmatterDiagnostic,
  type SkillFrontmatterParseInput,
  type SkillFrontmatterParseResponse,
  type SkillFrontmatterPatchInput,
  type SkillFrontmatterPatchResponse,
  type SkillActivationReadinessResponse,
} from "./skill-secret-declarations";
import {
  adminAssetListSchema,
  agentCapabilityBindingsInputSchema,
  agentCreateResponseSchema,
  agentInstructionsInputSchema,
  agentRuntimeAssessmentsInputSchema,
  agentRuntimeAssessmentsResponseSchema,
  agentVersionHistoryResponseSchema,
  agentVersionResponseSchema,
  assetIdSchema,
  assetKindSchema,
  assetListKindSchema,
  assetMutationResponseSchema,
  configuredMcpResponseSchema,
  createAgentInputSchema,
  createConfiguredMcpInputSchema,
  currentSystemBindingSchema,
  disableSystemBindingInputSchema,
  enableSystemBindingInputSchema,
  enableCurrentSystemBindingInputSchema,
  expectedAssetVersionInputSchema,
  expectedRevisionInputSchema,
  legacyCompatibleAdminAssetListSchema,
  legacyCompatibleAssetMutationResponseSchema,
  legacyCompatibleProjectAssetListSchema,
  moveSystemBindingInputSchema,
  mcpToolDiscoveryAttemptResponseSchema,
  mcpToolInventoryResponseSchema,
  mcpVersionHistoryResponseSchema,
  mcpVersionInputSchema,
  mcpVersionResponseSchema,
  projectAssetListSchema,
  projectDefaultAgentInputSchema,
  projectDefaultAgentSchema,
  projectMcpEditableConfigurationResponseSchema,
  projectSkillImportResponseSchema,
  skillActivationInputSchema,
  skillSecretReplaceInputSchema,
  skillSecretSetResponseSchema,
  secretClearInputSchema,
  mcpSecretReplaceInputSchema,
  mcpSecretSetResponseSchema,
  skillVersionHistoryResponseSchema,
  skillFileForkInputSchema,
  skillFilePathSchema,
  skillVersionInputSchema,
  skillVersionFileContentResponseSchema,
  skillVersionResponseSchema,
  syncCurrentSystemMcpBindingInputSchema,
  systemBindingSchema,
  updateConfiguredMcpInputSchema,
  type AdminAssetList,
  type AdminProjectAssetStatusAction,
  type AgentCapabilityBindingsInput,
  type AgentCreateResponse,
  type AgentInstructionsInput,
  type AgentRuntimeAssessmentsResponse,
  type AgentVersionResponse,
  type AssetKind,
  type AssetListKind,
  type AssetMutationResponse,
  type ConfiguredMcpResponse,
  type CreateAgentInput,
  type CreateConfiguredMcpInput,
  type DisableSystemBindingInput,
  type EnableSystemBindingInput,
  type EnableCurrentSystemBindingInput,
  type ExpectedAssetVersionInput,
  type ExpectedRevisionInput,
  type MoveSystemBindingInput,
  type SkillActivationInput,
  type McpToolDiscoveryAttemptResponse,
  type McpVersionInput,
  type McpToolInventoryResponse,
  type ProjectAssetList,
  type ProjectAssetStatusAction,
  type ProjectDefaultAgent,
  type ProjectDefaultAgentInput,
  type ProjectMcpEditableConfigurationResponse,
  type ProjectSkillImportResponse,
  type SkillSecretReplaceInput,
  type SkillSecretSetResponse,
  type McpSecretReplaceInput,
  type McpSecretSetResponse,
  type SecretClearInput,
  type SkillVersionInput,
  type SkillFileForkInput,
  type SkillVersionFileContentResponse,
  type SystemBinding,
  type SyncCurrentSystemMcpBindingInput,
  type UpdateConfiguredMcpInput,
  type VersionHistoryResponse,
  type VersionResponse,
} from "./types";

type MutableAssetListKind = AssetListKind;
type VersionedAssetListKind = MutableAssetListKind;

export type SkillDistributionDownload = {
  filename: string;
  content: Blob;
};

const SKILL_DISTRIBUTION_FILENAME_PATTERN =
  /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?-v[1-9][0-9]*\.zip$/u;
const SKILL_ARCHIVE_SECURITY_RISK_ACCEPTANCE =
  "accept-blocked-skill-archive" as const;

export const skillArchiveSecurityDiagnosticSchema = z
  .object({
    rule_id: z
      .string()
      .min(1)
      .max(128)
      .regex(/^[a-z0-9-]+$/u),
    file: z
      .string()
      .min(1)
      .max(1024)
      .refine((value) => !/[\u0000-\u001f\u007f]/u.test(value))
      .nullable(),
    line: z.number().int().positive().nullable(),
  })
  .strict();

export type SkillArchiveSecurityDiagnostic = z.infer<
  typeof skillArchiveSecurityDiagnosticSchema
>;

export const skillArchiveSecurityRiskConfirmationSchema = z
  .object({
    acceptance: z.literal(SKILL_ARCHIVE_SECURITY_RISK_ACCEPTANCE),
    payload_checksum: z.string().regex(/^[0-9a-f]{64}$/u),
    findings_checksum: z.string().regex(/^[0-9a-f]{64}$/u),
  })
  .strict();

export type SkillArchiveSecurityRiskConfirmation = z.infer<
  typeof skillArchiveSecurityRiskConfirmationSchema
>;

const serverErrorCodeSchema = z.enum([
  "asset_not_found",
  "asset_forbidden",
  "asset_conflict",
  "ASSET_IN_USE",
  "asset_validation_failed",
  "asset_storage_quota_exceeded",
  "asset_storage_unavailable",
  "SKILL_ARCHIVE_LIMIT_EXCEEDED",
  "SKILL_RUNTIME_NAME_CONFLICT",
  "SKILL_SECRET_DECLARATION_INVALID",
  "SKILL_FRONTMATTER_SOURCE_STALE",
  "SKILL_SECRETS_INCOMPLETE",
  "SKILL_SECRET_CONFIGURATION_INVALID",
  "SKILL_SECRET_REVISION_STALE",
]);

const standardErrorDetailSchema = z
  .object({
    code: serverErrorCodeSchema,
    message: z.string().min(1),
    request_id: z.string().min(1).optional(),
    diagnostics: z.array(skillFrontmatterDiagnosticSchema).optional(),
  })
  .strict();

const skillArchiveSecurityErrorDetailSchema = z
  .object({
    code: z.literal("SKILL_ARCHIVE_SECURITY_BLOCKED"),
    message: z.string().min(1),
    request_id: z.string().min(1).optional(),
    diagnostics: z.array(skillArchiveSecurityDiagnosticSchema).min(1).max(20),
    risk_confirmation: skillArchiveSecurityRiskConfirmationSchema.nullable(),
  })
  .strict();

const errorEnvelopeSchema = z
  .object({
    detail: z.union([
      skillArchiveSecurityErrorDetailSchema,
      standardErrorDetailSchema,
    ]),
  })
  .strict();

const SAFE_SERVER_ERRORS = {
  asset_not_found: ["ASSET_NOT_FOUND", "Asset not found"],
  asset_forbidden: ["ASSET_FORBIDDEN", "Asset capability required"],
  asset_conflict: ["ASSET_CONFLICT", "Asset state conflict"],
  ASSET_IN_USE: ["ASSET_IN_USE", "Asset is still referenced"],
  asset_validation_failed: [
    "ASSET_VALIDATION_FAILED",
    "Asset validation failed",
  ],
  asset_storage_quota_exceeded: [
    "ASSET_STORAGE_QUOTA_EXCEEDED",
    "Project Skill storage quota exceeded",
  ],
  asset_storage_unavailable: [
    "ASSET_STORAGE_UNAVAILABLE",
    "Asset storage unavailable",
  ],
  SKILL_ARCHIVE_LIMIT_EXCEEDED: [
    "ASSET_UPLOAD_TOO_LARGE",
    "Skill archive exceeds the allowed size or member limit",
  ],
  SKILL_ARCHIVE_SECURITY_BLOCKED: [
    "SKILL_ARCHIVE_SECURITY_BLOCKED",
    "Skill archive failed security scan",
  ],
  SKILL_RUNTIME_NAME_CONFLICT: [
    "SKILL_RUNTIME_NAME_CONFLICT",
    "Skill runtime name conflict",
  ],
  SKILL_SECRET_DECLARATION_INVALID: [
    "SKILL_SECRET_DECLARATION_INVALID",
    "Skill secret declaration is invalid",
  ],
  SKILL_FRONTMATTER_SOURCE_STALE: [
    "SKILL_FRONTMATTER_SOURCE_STALE",
    "Skill frontmatter source changed",
  ],
  SKILL_SECRETS_INCOMPLETE: [
    "SKILL_SECRETS_INCOMPLETE",
    "Required Skill secrets are incomplete",
  ],
  SKILL_SECRET_CONFIGURATION_INVALID: [
    "SKILL_SECRET_CONFIGURATION_INVALID",
    "Skill secret configuration is invalid",
  ],
  SKILL_SECRET_REVISION_STALE: [
    "SKILL_SECRET_REVISION_STALE",
    "Skill secret revision is stale",
  ],
} as const;

export const SHARED_ASSET_ERROR_CODES = [
  "ASSET_NOT_FOUND",
  "ASSET_FORBIDDEN",
  "ASSET_CONFLICT",
  "ASSET_IN_USE",
  "ASSET_VALIDATION_FAILED",
  "ASSET_STORAGE_QUOTA_EXCEEDED",
  "ASSET_STORAGE_UNAVAILABLE",
  "SKILL_RUNTIME_NAME_CONFLICT",
  "SKILL_SECRET_DECLARATION_INVALID",
  "SKILL_FRONTMATTER_SOURCE_STALE",
  "SKILL_SECRETS_INCOMPLETE",
  "SKILL_SECRET_CONFIGURATION_INVALID",
  "SKILL_SECRET_REVISION_STALE",
  "ASSET_UPLOAD_TOO_LARGE",
  "SKILL_ARCHIVE_SECURITY_BLOCKED",
  "AUTH_REQUIRED",
  "ASSET_NETWORK_ERROR",
  "ASSET_RESPONSE_INVALID",
  "ASSET_ERROR_RESPONSE_INVALID",
] as const;

export type SharedAssetErrorCode = (typeof SHARED_ASSET_ERROR_CODES)[number];

export class SharedAssetApiError extends Error {
  readonly status: number;
  readonly code: SharedAssetErrorCode;
  readonly diagnostics: readonly SkillFrontmatterDiagnostic[] | undefined;
  readonly skillArchiveSecurityDiagnostics:
    | readonly SkillArchiveSecurityDiagnostic[]
    | undefined;
  readonly skillArchiveSecurityRiskConfirmation:
    | SkillArchiveSecurityRiskConfirmation
    | null
    | undefined;

  constructor(
    status: number,
    code: SharedAssetErrorCode,
    message: string,
    diagnostics?: readonly SkillFrontmatterDiagnostic[],
    skillArchiveSecurityDiagnostics?: readonly SkillArchiveSecurityDiagnostic[],
    skillArchiveSecurityRiskConfirmation?: SkillArchiveSecurityRiskConfirmation | null,
  ) {
    super(message);
    this.name = "SharedAssetApiError";
    this.status = status;
    this.code = code;
    this.diagnostics = diagnostics;
    this.skillArchiveSecurityDiagnostics = skillArchiveSecurityDiagnostics;
    this.skillArchiveSecurityRiskConfirmation =
      skillArchiveSecurityRiskConfirmation;
  }
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new SharedAssetApiError(
      response.status,
      response.ok ? "ASSET_RESPONSE_INVALID" : "ASSET_ERROR_RESPONSE_INVALID",
      response.ok
        ? "Shared asset response was invalid"
        : "Shared asset request failed",
    );
  }
}

async function request(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (error instanceof SharedAssetApiError || isAbortError(error))
      throw error;
    if (error instanceof AuthRequiredError) {
      throw new SharedAssetApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new SharedAssetApiError(
      0,
      "ASSET_NETWORK_ERROR",
      "Shared asset service is unavailable",
    );
  }
}

function parseInput<T>(schema: z.ZodType<T>, value: unknown): T {
  const parsed = schema.safeParse(value);
  if (!parsed.success) {
    throw new SharedAssetApiError(
      422,
      "ASSET_VALIDATION_FAILED",
      "Asset validation failed",
    );
  }
  return parsed.data;
}

async function throwResponseError(response: Response): Promise<never> {
  const parsed = errorEnvelopeSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new SharedAssetApiError(
      response.status,
      "ASSET_ERROR_RESPONSE_INVALID",
      "Shared asset request failed",
    );
  }
  const [code, message] = SAFE_SERVER_ERRORS[parsed.data.detail.code];
  const archiveSecurityDiagnostics =
    parsed.data.detail.code === "SKILL_ARCHIVE_SECURITY_BLOCKED"
      ? parsed.data.detail.diagnostics
      : undefined;
  const frontmatterDiagnostics =
    parsed.data.detail.code === "SKILL_ARCHIVE_SECURITY_BLOCKED"
      ? undefined
      : parsed.data.detail.diagnostics;
  const archiveSecurityRiskConfirmation =
    parsed.data.detail.code === "SKILL_ARCHIVE_SECURITY_BLOCKED"
      ? parsed.data.detail.risk_confirmation
      : undefined;
  throw new SharedAssetApiError(
    response.status,
    code,
    message,
    frontmatterDiagnostics,
    archiveSecurityDiagnostics,
    archiveSecurityRiskConfirmation,
  );
}

async function parseResponse<T>(
  response: Response,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
): Promise<T> {
  if (!response.ok) await throwResponseError(response);
  const parsed = schema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new SharedAssetApiError(
      response.status,
      "ASSET_RESPONSE_INVALID",
      "Shared asset response was invalid",
    );
  }
  return parsed.data;
}

function skillDistributionFilename(response: Response): string | null {
  const disposition = response.headers.get("Content-Disposition");
  const match = disposition?.match(/^attachment;\s*filename="([^"]+)"$/iu);
  const filename = match?.[1] ?? null;
  return filename && SKILL_DISTRIBUTION_FILENAME_PATTERN.test(filename)
    ? filename
    : null;
}

async function parseSkillDistributionDownload(
  response: Response,
): Promise<SkillDistributionDownload> {
  if (!response.ok) await throwResponseError(response);
  const contentType = response.headers.get("Content-Type")?.split(";", 1)[0];
  const filename = skillDistributionFilename(response);
  if (contentType !== "application/zip" || !filename) {
    throw new SharedAssetApiError(
      response.status,
      "ASSET_RESPONSE_INVALID",
      "Shared asset response was invalid",
    );
  }
  try {
    const content = await response.blob();
    if (content.size === 0) throw new Error("Empty Skill distribution package");
    return { filename, content };
  } catch (error) {
    if (error instanceof SharedAssetApiError) throw error;
    throw new SharedAssetApiError(
      response.status,
      "ASSET_RESPONSE_INVALID",
      "Shared asset response was invalid",
    );
  }
}

function projectAssetUrl(projectId: string, kind: AssetListKind): string {
  const parsedProjectId = parseInput(assetIdSchema, projectId);
  const parsedKind = parseInput(assetListKindSchema, kind);
  return `${getBackendBaseURL()}/api/projects/${parsedProjectId}/${parsedKind}`;
}

function projectDefaultAgentUrl(projectId: string): string {
  const parsedProjectId = parseInput(assetIdSchema, projectId);
  return `${getBackendBaseURL()}/api/projects/${parsedProjectId}/default-agent`;
}

function adminAssetUrl(kind: AssetListKind): string {
  return `${getBackendBaseURL()}/api/admin/assets/${parseInput(
    assetListKindSchema,
    kind,
  )}`;
}

function adminProjectAssetUrl(projectId: string, kind: AssetListKind): string {
  const parsedProjectId = parseInput(assetIdSchema, projectId);
  const parsedKind = parseInput(assetListKindSchema, kind);
  return `${getBackendBaseURL()}/api/admin/projects/${parsedProjectId}/assets/${parsedKind}`;
}

function systemCatalogUrl(kind: AssetListKind): string {
  return `${getBackendBaseURL()}/api/assets/catalog/${parseInput(
    assetListKindSchema,
    kind,
  )}`;
}

function versionHistorySchema(
  kind: AssetListKind,
): z.ZodType<VersionHistoryResponse> {
  if (kind === "agents") return agentVersionHistoryResponseSchema;
  if (kind === "skills") return skillVersionHistoryResponseSchema;
  return mcpVersionHistoryResponseSchema;
}

function versionResponseSchema(
  kind: VersionedAssetListKind,
): z.ZodType<VersionResponse> {
  if (kind === "agents") return agentVersionResponseSchema;
  if (kind === "skills") return skillVersionResponseSchema;
  return mcpVersionResponseSchema;
}

export async function listProjectAssets(
  projectId: string,
  kind: AssetListKind,
  signal?: AbortSignal,
): Promise<ProjectAssetList> {
  const response = await request(projectAssetUrl(projectId, kind), { signal });
  return parseResponse(
    response,
    kind === "mcp-servers"
      ? legacyCompatibleProjectAssetListSchema
      : projectAssetListSchema,
  );
}

export async function getProjectDefaultAgent(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectDefaultAgent> {
  return parseResponse(
    await request(projectDefaultAgentUrl(projectId), { signal }),
    projectDefaultAgentSchema,
  );
}

export async function setProjectDefaultAgent(
  projectId: string,
  input: ProjectDefaultAgentInput,
  signal?: AbortSignal,
): Promise<ProjectDefaultAgent> {
  const body = parseInput(projectDefaultAgentInputSchema, input);
  return parseResponse(
    await request(projectDefaultAgentUrl(projectId), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    }),
    projectDefaultAgentSchema,
  );
}

export async function listAdminAssets(
  kind: AssetListKind,
  signal?: AbortSignal,
): Promise<AdminAssetList> {
  const response = await request(adminAssetUrl(kind), { signal });
  return parseResponse(
    response,
    kind === "mcp-servers"
      ? legacyCompatibleAdminAssetListSchema
      : adminAssetListSchema,
  );
}

export async function listAdminProjectAssets(
  projectId: string,
  kind: AssetListKind,
  signal?: AbortSignal,
): Promise<ProjectAssetList> {
  const response = await request(adminProjectAssetUrl(projectId, kind), {
    signal,
  });
  return parseResponse(
    response,
    kind === "mcp-servers"
      ? legacyCompatibleProjectAssetListSchema
      : projectAssetListSchema,
  );
}

export async function listSystemAssetCatalog(
  kind: AssetListKind,
  signal?: AbortSignal,
): Promise<AdminAssetList> {
  const response = await request(systemCatalogUrl(kind), { signal });
  return parseResponse(
    response,
    kind === "mcp-servers"
      ? legacyCompatibleAdminAssetListSchema
      : adminAssetListSchema,
  );
}

export async function createProjectAgent(
  projectId: string,
  input: CreateAgentInput,
  signal?: AbortSignal,
): Promise<AgentCreateResponse> {
  const body = parseInput(createAgentInputSchema, input);
  const response = await request(projectAssetUrl(projectId, "agents"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(
    response,
    agentCreateResponseSchema.superRefine((value, context) => {
      if (value.item.project_id !== projectId) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["item", "project_id"],
          message: "Created Agent must belong to the requested project",
        });
      }
    }),
  );
}

function configuredMcpResponseSchemaForRequest({
  projectId,
  assetId,
  expectedAssetVersion,
  secretSlotNames,
}: {
  projectId: string;
  assetId?: string;
  expectedAssetVersion?: number;
  secretSlotNames: readonly string[];
}) {
  return configuredMcpResponseSchema.superRefine((value, context) => {
    if (value.item.scope !== "project" || value.item.project_id !== projectId) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Configured MCP response must belong to the requested project",
        path: ["item", "project_id"],
      });
    }
    if (assetId !== undefined && value.item.id !== assetId) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Configured MCP response must match the requested asset",
        path: ["item", "id"],
      });
    }
    if (
      expectedAssetVersion !== undefined &&
      value.item.version !== expectedAssetVersion + 2
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Configured MCP update must advance the asset revision exactly twice",
        path: ["item", "version"],
      });
    }

    const responseSlotNames = value.version.secret_slots.map(
      (slot) => slot.name,
    );
    if (
      responseSlotNames.length !== secretSlotNames.length ||
      secretSlotNames.some((name) => !responseSlotNames.includes(name))
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Configured MCP response secret slots must match the request",
        path: ["version", "secret_slots"],
      });
    }

    if (value.version.workflow_status !== "published") {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Configured MCP response must be published",
        path: ["version", "workflow_status"],
      });
    }
  });
}

function projectMcpEditableConfigurationSchemaForRequest(
  projectId: string,
  assetId: string,
) {
  return projectMcpEditableConfigurationResponseSchema.superRefine(
    (value, context) => {
      if (
        value.item.project_id !== projectId ||
        value.item.id !== assetId ||
        value.version.mcp_server_id !== assetId
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message:
            "Editable MCP configuration must match the requested Project asset",
          path: ["item", "id"],
        });
      }
    },
  );
}

export async function getProjectMcpEditableConfiguration(
  projectId: string,
  assetId: string,
  signal?: AbortSignal,
): Promise<ProjectMcpEditableConfigurationResponse> {
  const parsedProjectId = parseInput(assetIdSchema, projectId);
  const id = parseInput(assetIdSchema, assetId);
  const response = await request(
    `${projectAssetUrl(parsedProjectId, "mcp-servers")}/${id}/configured`,
    { signal },
  );
  return parseResponse(
    response,
    projectMcpEditableConfigurationSchemaForRequest(parsedProjectId, id),
  );
}

export async function getProjectMcpSecrets(
  projectId: string,
  assetId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<McpSecretSetResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return parseResponse(
    await request(
      `${projectAssetUrl(projectId, "mcp-servers")}/${asset}/versions/${version}/secrets`,
      { signal },
    ),
    mcpSecretSetResponseSchema,
  );
}

export async function replaceProjectMcpSecret(
  projectId: string,
  assetId: string,
  versionId: string,
  slotName: string,
  input: McpSecretReplaceInput,
  signal?: AbortSignal,
): Promise<McpSecretSetResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  const slot = parseInput(z.string().min(1).max(63), slotName);
  const body = parseInput(mcpSecretReplaceInputSchema, input);
  return parseResponse(
    await request(
      `${projectAssetUrl(projectId, "mcp-servers")}/${asset}/versions/${version}/secrets/${encodeURIComponent(slot)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      },
    ),
    mcpSecretSetResponseSchema,
  );
}

export async function clearProjectMcpSecret(
  projectId: string,
  assetId: string,
  versionId: string,
  slotName: string,
  input: SecretClearInput,
  signal?: AbortSignal,
): Promise<McpSecretSetResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  const slot = parseInput(z.string().min(1).max(63), slotName);
  const body = parseInput(secretClearInputSchema, input);
  return parseResponse(
    await request(
      `${projectAssetUrl(projectId, "mcp-servers")}/${asset}/versions/${version}/secrets/${encodeURIComponent(slot)}/clear`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      },
    ),
    mcpSecretSetResponseSchema,
  );
}

export async function createConfiguredProjectMcp(
  projectId: string,
  input: CreateConfiguredMcpInput,
  signal?: AbortSignal,
): Promise<ConfiguredMcpResponse> {
  const parsedProjectId = parseInput(assetIdSchema, projectId);
  const body = parseInput(createConfiguredMcpInputSchema, input);
  const response = await request(
    `${projectAssetUrl(parsedProjectId, "mcp-servers")}/configured`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return parseResponse(
    response,
    configuredMcpResponseSchemaForRequest({
      projectId: parsedProjectId,
      secretSlotNames: (body.secret_slots ?? []).map((slot) => slot.name),
    }),
  );
}

export async function updateConfiguredProjectMcp(
  projectId: string,
  assetId: string,
  input: UpdateConfiguredMcpInput,
  signal?: AbortSignal,
): Promise<ConfiguredMcpResponse> {
  const parsedProjectId = parseInput(assetIdSchema, projectId);
  const id = parseInput(assetIdSchema, assetId);
  const body = parseInput(updateConfiguredMcpInputSchema, input);
  const response = await request(
    `${projectAssetUrl(parsedProjectId, "mcp-servers")}/${id}/configured`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return parseResponse(
    response,
    configuredMcpResponseSchemaForRequest({
      projectId: parsedProjectId,
      assetId: id,
      expectedAssetVersion: body.expected_asset_version,
      secretSlotNames: (body.secret_slots ?? []).map((slot) => slot.name),
    }),
  );
}

export async function updateProjectAgentInstructions(
  projectId: string,
  assetId: string,
  input: AgentInstructionsInput,
  signal?: AbortSignal,
): Promise<AgentVersionResponse> {
  const id = parseInput(assetIdSchema, assetId);
  const body = parseInput(agentInstructionsInputSchema, input);
  const response = await request(
    `${projectAssetUrl(projectId, "agents")}/${id}/instructions`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return parseResponse(response, agentVersionResponseSchema);
}

export async function updateProjectAgentCapabilityBindings(
  projectId: string,
  assetId: string,
  input: AgentCapabilityBindingsInput,
  signal?: AbortSignal,
): Promise<AgentVersionResponse> {
  const id = parseInput(assetIdSchema, assetId);
  const body = parseInput(agentCapabilityBindingsInputSchema, input);
  const response = await request(
    `${projectAssetUrl(projectId, "agents")}/${id}/capability-bindings`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return parseResponse(response, agentVersionResponseSchema);
}

export async function importProjectSkillArchive(
  projectId: string,
  archive: File,
  options: {
    securityRiskConfirmation?: SkillArchiveSecurityRiskConfirmation;
    signal?: AbortSignal;
  } = {},
): Promise<ProjectSkillImportResponse> {
  if (archive.name.trim() === "" || archive.size === 0) {
    throw new SharedAssetApiError(
      422,
      "ASSET_VALIDATION_FAILED",
      "Asset validation failed",
    );
  }
  const body = new FormData();
  body.append("archive", archive, archive.name);
  if (options.securityRiskConfirmation) {
    body.append(
      "security_risk_acceptance",
      options.securityRiskConfirmation.acceptance,
    );
    body.append(
      "security_risk_payload_checksum",
      options.securityRiskConfirmation.payload_checksum,
    );
    body.append(
      "security_risk_findings_checksum",
      options.securityRiskConfirmation.findings_checksum,
    );
  }
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/import`,
    {
      method: "POST",
      body,
      signal: options.signal,
    },
  );
  if (response.status === 413) {
    throw new SharedAssetApiError(
      413,
      "ASSET_UPLOAD_TOO_LARGE",
      "Skill archive upload too large",
    );
  }
  return parseResponse(response, projectSkillImportResponseSchema);
}

export async function exportProjectSkillVersion(
  projectId: string,
  skillId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<SkillDistributionDownload> {
  const skill = parseInput(assetIdSchema, skillId);
  const version = parseInput(assetIdSchema, versionId);
  return parseSkillDistributionDownload(
    await request(
      `${projectAssetUrl(projectId, "skills")}/${skill}/versions/${version}/export`,
      { signal },
    ),
  );
}

export async function exportAdminSkillVersion(
  skillId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<SkillDistributionDownload> {
  const skill = parseInput(assetIdSchema, skillId);
  const version = parseInput(assetIdSchema, versionId);
  return parseSkillDistributionDownload(
    await request(
      `${adminAssetUrl("skills")}/${skill}/versions/${version}/export`,
      { signal },
    ),
  );
}

export async function parseProjectSkillFrontmatter(
  projectId: string,
  input: SkillFrontmatterParseInput,
  signal?: AbortSignal,
): Promise<SkillFrontmatterParseResponse> {
  const body = parseInput(skillFrontmatterParseInputSchema, input);
  return parseResponse(
    await request(`${projectAssetUrl(projectId, "skills")}/frontmatter/parse`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    }),
    skillFrontmatterParseResponseSchema,
  );
}

export async function patchProjectSkillFrontmatter(
  projectId: string,
  input: SkillFrontmatterPatchInput,
  signal?: AbortSignal,
): Promise<SkillFrontmatterPatchResponse> {
  const body = parseInput(skillFrontmatterPatchInputSchema, input);
  return parseResponse(
    await request(`${projectAssetUrl(projectId, "skills")}/frontmatter/patch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    }),
    skillFrontmatterPatchResponseSchema,
  );
}

export async function getProjectSkillActivationReadiness(
  projectId: string,
  skillId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<SkillActivationReadinessResponse> {
  const skill = parseInput(assetIdSchema, skillId);
  const version = parseInput(assetIdSchema, versionId);
  return parseResponse(
    await request(
      `${projectAssetUrl(projectId, "skills")}/${skill}/versions/${version}/activation-readiness`,
      { signal },
    ),
    skillActivationReadinessResponseSchema.superRefine((value, context) => {
      if (value.skill_id !== skill) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill activation readiness belongs to another Skill",
          path: ["skill_id"],
        });
      }
      if (value.skill_version_id !== version) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill activation readiness belongs to another version",
          path: ["skill_version_id"],
        });
      }
    }),
  );
}

export async function createAdminProjectAgent(
  projectId: string,
  input: CreateAgentInput,
  signal?: AbortSignal,
): Promise<AgentCreateResponse> {
  const body = parseInput(createAgentInputSchema, input);
  const response = await request(adminProjectAssetUrl(projectId, "agents"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(
    response,
    agentCreateResponseSchema.superRefine((value, context) => {
      if (value.item.project_id !== projectId) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["item", "project_id"],
          message: "Created Agent must belong to the requested project",
        });
      }
    }),
  );
}

type AssetVersionAuthoringInput = SkillVersionInput | McpVersionInput;

function authoringInputSchema(
  kind: VersionedAssetListKind,
): z.ZodType<unknown> {
  if (kind === "skills") return skillVersionInputSchema;
  return mcpVersionInputSchema;
}

export function createProjectAssetVersion(
  projectId: string,
  kind: "skills",
  assetId: string,
  input: SkillVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse>;
export function createProjectAssetVersion(
  projectId: string,
  kind: "mcp-servers",
  assetId: string,
  input: McpVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse>;
export function createProjectAssetVersion(
  projectId: string,
  kind: VersionedAssetListKind,
  assetId: string,
  input: AssetVersionAuthoringInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const id = parseInput(assetIdSchema, assetId);
  return postVersionMutation(
    `${projectAssetUrl(projectId, kind)}/${id}/versions`,
    authoringInputSchema(kind),
    versionResponseSchema(kind),
    input,
    signal,
  );
}

export function createAdminProjectAssetVersion(
  projectId: string,
  kind: "skills",
  assetId: string,
  input: SkillVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse>;
export function createAdminProjectAssetVersion(
  projectId: string,
  kind: "mcp-servers",
  assetId: string,
  input: McpVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse>;
export function createAdminProjectAssetVersion(
  projectId: string,
  kind: VersionedAssetListKind,
  assetId: string,
  input: AssetVersionAuthoringInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const id = parseInput(assetIdSchema, assetId);
  return postVersionMutation(
    `${adminProjectAssetUrl(projectId, kind)}/${id}/versions`,
    authoringInputSchema(kind),
    versionResponseSchema(kind),
    input,
    signal,
  );
}

async function changeAssetStatus(
  url: string,
  input: ExpectedAssetVersionInput | ExpectedRevisionInput,
  currentVersionContract: boolean,
  signal?: AbortSignal,
): Promise<AssetMutationResponse> {
  const body = currentVersionContract
    ? parseInput(expectedRevisionInputSchema, input)
    : parseInput(expectedAssetVersionInputSchema, input);
  const response = await request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(
    response,
    currentVersionContract
      ? assetMutationResponseSchema
      : legacyCompatibleAssetMutationResponseSchema,
  );
}

export function changeProjectAssetStatus<Kind extends MutableAssetListKind>(
  projectId: string,
  kind: Kind,
  assetId: string,
  action: ProjectAssetStatusAction<Kind>,
  input: ExpectedAssetVersionInput | ExpectedRevisionInput,
  signal?: AbortSignal,
) {
  const currentVersionContract = kind === "agents" || kind === "skills";
  const validAction = currentVersionContract
    ? action === "enable" || action === "suspend"
    : action === "activate" || action === "suspend";
  if (!validAction) {
    throw new SharedAssetApiError(
      422,
      "ASSET_VALIDATION_FAILED",
      "Asset validation failed",
    );
  }
  const id = parseInput(assetIdSchema, assetId);
  return changeAssetStatus(
    `${projectAssetUrl(projectId, kind)}/${id}/${action}`,
    input,
    currentVersionContract,
    signal,
  );
}

export function changeAdminProjectAssetStatus<
  Kind extends MutableAssetListKind,
>(
  projectId: string,
  kind: Kind,
  assetId: string,
  action: AdminProjectAssetStatusAction<Kind>,
  input: ExpectedAssetVersionInput | ExpectedRevisionInput,
  signal?: AbortSignal,
) {
  const validAction =
    kind === "skills" || kind === "agents"
      ? action === "enable" || action === "suspend"
      : action === "archive" || action === "suspend";
  if (!validAction) {
    throw new SharedAssetApiError(
      422,
      "ASSET_VALIDATION_FAILED",
      "Asset validation failed",
    );
  }
  const id = parseInput(assetIdSchema, assetId);
  return changeAssetStatus(
    `${adminProjectAssetUrl(projectId, kind)}/${id}/${action}`,
    input,
    kind === "skills" || kind === "agents",
    signal,
  );
}

export async function deleteProjectSkill(
  projectId: string,
  assetId: string,
  input: ExpectedRevisionInput,
  signal?: AbortSignal,
): Promise<void> {
  const id = parseInput(assetIdSchema, assetId);
  const body = parseInput(expectedRevisionInputSchema, input);
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/${id}`,
    {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  if (!response.ok) await throwResponseError(response);
}

export async function deleteProjectAgent(
  projectId: string,
  assetId: string,
  input: ExpectedRevisionInput,
  signal?: AbortSignal,
): Promise<void> {
  const id = parseInput(assetIdSchema, assetId);
  const body = parseInput(expectedRevisionInputSchema, input);
  const response = await request(
    `${projectAssetUrl(projectId, "agents")}/${id}`,
    {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  if (!response.ok) await throwResponseError(response);
}

export async function deleteProjectMcp(
  projectId: string,
  assetId: string,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<void> {
  const id = parseInput(assetIdSchema, assetId);
  const body = parseInput(expectedAssetVersionInputSchema, input);
  const response = await request(
    `${projectAssetUrl(projectId, "mcp-servers")}/${id}`,
    {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  if (!response.ok) await throwResponseError(response);
}

export async function listProjectAssetVersions(
  projectId: string,
  kind: AssetListKind,
  assetId: string,
  signal?: AbortSignal,
): Promise<VersionHistoryResponse> {
  const id = parseInput(assetIdSchema, assetId);
  const response = await request(
    `${projectAssetUrl(projectId, kind)}/${id}/versions`,
    { signal },
  );
  return parseResponse(response, versionHistorySchema(kind));
}

export async function listSystemAssetVersions(
  kind: Exclude<AssetListKind, "mcp-servers">,
  assetId: string,
  signal?: AbortSignal,
): Promise<VersionHistoryResponse> {
  const id = parseInput(assetIdSchema, assetId);
  const response = await request(`${systemCatalogUrl(kind)}/${id}/versions`, {
    signal,
  });
  return parseResponse(response, versionHistorySchema(kind));
}

export async function assessProjectAgentRuntime(
  projectId: string,
  agentIds: readonly string[],
  signal?: AbortSignal,
): Promise<AgentRuntimeAssessmentsResponse> {
  const body = parseInput(agentRuntimeAssessmentsInputSchema, {
    agent_ids: [...agentIds],
  });
  const response = await request(
    `${projectAssetUrl(projectId, "agents")}/runtime-assessments`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return parseResponse(
    response,
    agentRuntimeAssessmentsResponseSchema.superRefine((value, context) => {
      if (
        value.items.length !== body.agent_ids.length ||
        value.items.some(
          (item, index) => item.agent_asset_id !== body.agent_ids[index],
        )
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message:
            "Agent runtime assessments must match the requested input order",
          path: ["items"],
        });
      }
    }),
  );
}

export async function getProjectMcpToolInventory(
  projectId: string,
  assetId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<McpToolInventoryResponse> {
  const parsedAssetId = parseInput(assetIdSchema, assetId);
  const parsedVersionId = parseInput(assetIdSchema, versionId);
  const response = await request(
    `${projectAssetUrl(projectId, "mcp-servers")}/${parsedAssetId}/versions/${parsedVersionId}/tools`,
    { signal },
  );
  return parseResponse(response, mcpToolInventoryResponseSchema);
}

export async function requestProjectMcpToolDiscovery(
  projectId: string,
  assetId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<McpToolDiscoveryAttemptResponse> {
  const parsedAssetId = parseInput(assetIdSchema, assetId);
  const parsedVersionId = parseInput(assetIdSchema, versionId);
  const response = await request(
    `${projectAssetUrl(projectId, "mcp-servers")}/${parsedAssetId}/versions/${parsedVersionId}/tool-discovery`,
    { method: "POST", signal },
  );
  return parseResponse(
    response,
    mcpToolDiscoveryAttemptResponseSchema.superRefine((value, context) => {
      if (value.data.mcp_server_id !== parsedAssetId) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "MCP tool discovery attempt must match the requested asset",
          path: ["data", "mcp_server_id"],
        });
      }
      if (value.data.mcp_server_version_id !== parsedVersionId) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message:
            "MCP tool discovery attempt must match the requested version",
          path: ["data", "mcp_server_version_id"],
        });
      }
    }),
  );
}

export async function listAdminAssetVersions(
  kind: AssetListKind,
  assetId: string,
  signal?: AbortSignal,
): Promise<VersionHistoryResponse> {
  const id = parseInput(assetIdSchema, assetId);
  const response = await request(`${adminAssetUrl(kind)}/${id}/versions`, {
    signal,
  });
  return parseResponse(response, versionHistorySchema(kind));
}

export async function listAdminProjectAssetVersions(
  projectId: string,
  kind: AssetListKind,
  assetId: string,
  signal?: AbortSignal,
): Promise<VersionHistoryResponse> {
  const id = parseInput(assetIdSchema, assetId);
  const response = await request(
    `${adminProjectAssetUrl(projectId, kind)}/${id}/versions`,
    { signal },
  );
  return parseResponse(response, versionHistorySchema(kind));
}

export async function getProjectSkillVersionFile(
  projectId: string,
  assetId: string,
  versionId: string,
  path: string,
  signal?: AbortSignal,
): Promise<SkillVersionFileContentResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  const filePath = parseInput(skillFilePathSchema, path);
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/${asset}/versions/${version}/files/content?path=${encodeURIComponent(filePath)}`,
    { signal },
  );
  return parseResponse(response, skillVersionFileContentResponseSchema);
}

export async function getProjectSkillSecrets(
  projectId: string,
  skillId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<SkillSecretSetResponse> {
  const skill = parseInput(assetIdSchema, skillId);
  const version = parseInput(assetIdSchema, versionId);
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/${skill}/versions/${version}/secrets`,
    { signal },
  );
  return parseResponse(
    response,
    skillSecretSetResponseSchema.superRefine((value, context) => {
      if (value.skill_id !== skill) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill secrets belong to another Skill",
          path: ["skill_id"],
        });
      }
      if (value.skill_version_id !== version) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill secrets belong to another version",
          path: ["skill_version_id"],
        });
      }
    }),
  );
}

export async function replaceProjectSkillSecrets(
  projectId: string,
  skillId: string,
  versionId: string,
  input: SkillSecretReplaceInput,
  signal?: AbortSignal,
): Promise<SkillSecretSetResponse> {
  const skill = parseInput(assetIdSchema, skillId);
  const version = parseInput(assetIdSchema, versionId);
  const body = parseInput(skillSecretReplaceInputSchema, input);
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/${skill}/versions/${version}/secrets`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return parseResponse(
    response,
    skillSecretSetResponseSchema.superRefine((value, context) => {
      if (value.skill_id !== skill) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill secrets belong to another Skill",
          path: ["skill_id"],
        });
      }
      if (value.skill_version_id !== version) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill secrets belong to another version",
          path: ["skill_version_id"],
        });
      }
    }),
  );
}

export async function clearProjectSkillSecret(
  projectId: string,
  skillId: string,
  versionId: string,
  secretName: string,
  input: SecretClearInput,
  signal?: AbortSignal,
): Promise<SkillSecretSetResponse> {
  const skill = parseInput(assetIdSchema, skillId);
  const version = parseInput(assetIdSchema, versionId);
  const name = parseInput(z.string().min(1).max(255), secretName);
  const body = parseInput(secretClearInputSchema, input);
  return parseResponse(
    await request(
      `${projectAssetUrl(projectId, "skills")}/${skill}/versions/${version}/secrets/${encodeURIComponent(name)}/clear`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      },
    ),
    skillSecretSetResponseSchema,
  );
}

async function postVersionMutation<TInput, TResponse = VersionResponse>(
  url: string,
  inputSchema: z.ZodType<TInput>,
  responseSchema: z.ZodType<TResponse>,
  input: unknown,
  signal?: AbortSignal,
): Promise<TResponse> {
  const body = parseInput(inputSchema, input);
  const response = await request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, responseSchema);
}

export function forkProjectSkillVersion(
  projectId: string,
  assetId: string,
  sourceVersionId: string,
  input: SkillFileForkInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const sourceVersion = parseInput(assetIdSchema, sourceVersionId);
  return postVersionMutation(
    `${projectAssetUrl(projectId, "skills")}/${asset}/versions/${sourceVersion}/fork`,
    skillFileForkInputSchema,
    skillVersionResponseSchema,
    input,
    signal,
  );
}

export function activateProjectAssetVersion(
  projectId: string,
  kind: "skills",
  assetId: string,
  versionId: string,
  input: SkillActivationInput,
  signal?: AbortSignal,
): Promise<VersionResponse>;
export function activateProjectAssetVersion(
  projectId: string,
  kind: "agents",
  assetId: string,
  versionId: string,
  input: ExpectedRevisionInput,
  signal?: AbortSignal,
): Promise<VersionResponse>;
export function activateProjectAssetVersion(
  projectId: string,
  kind: "agents" | "skills",
  assetId: string,
  versionId: string,
  input: ExpectedRevisionInput | SkillActivationInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return postVersionMutation(
    `${projectAssetUrl(projectId, kind)}/${asset}/versions/${version}/activate`,
    kind === "skills"
      ? skillActivationInputSchema
      : expectedRevisionInputSchema,
    versionResponseSchema(kind),
    input,
    signal,
  );
}

export function activateAdminProjectAssetVersion(
  projectId: string,
  kind: "agents" | "skills",
  assetId: string,
  versionId: string,
  input: ExpectedRevisionInput | SkillActivationInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return postVersionMutation(
    `${adminProjectAssetUrl(projectId, kind)}/${asset}/versions/${version}/activate`,
    kind === "skills"
      ? skillActivationInputSchema
      : expectedRevisionInputSchema,
    versionResponseSchema(kind),
    input,
    signal,
  );
}

export function publishProjectMcpVersion(
  projectId: string,
  assetId: string,
  versionId: string,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return postVersionMutation(
    `${projectAssetUrl(projectId, "mcp-servers")}/${asset}/versions/${version}/publish`,
    expectedAssetVersionInputSchema,
    mcpVersionResponseSchema,
    input,
    signal,
  );
}

export function publishAdminProjectMcpVersion(
  projectId: string,
  assetId: string,
  versionId: string,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return postVersionMutation(
    `${adminProjectAssetUrl(projectId, "mcp-servers")}/${asset}/versions/${version}/publish`,
    expectedAssetVersionInputSchema,
    mcpVersionResponseSchema,
    input,
    signal,
  );
}

function projectMcpVersionUrl(
  projectId: string,
  assetId: string,
  versionId: string,
): string {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return `${projectAssetUrl(projectId, "mcp-servers")}/${asset}/versions/${version}`;
}

export function submitProjectMcpVersion(
  projectId: string,
  assetId: string,
  versionId: string,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  return postVersionMutation(
    `${projectMcpVersionUrl(projectId, assetId, versionId)}/submit-approval`,
    expectedAssetVersionInputSchema,
    mcpVersionResponseSchema,
    input,
    signal,
  );
}

function adminProjectMcpVersionUrl(
  projectId: string,
  assetId: string,
  versionId: string,
): string {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return `${adminProjectAssetUrl(projectId, "mcp-servers")}/${asset}/versions/${version}`;
}

export function submitAdminProjectMcpVersion(
  projectId: string,
  assetId: string,
  versionId: string,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  return postVersionMutation(
    `${adminProjectMcpVersionUrl(projectId, assetId, versionId)}/submit-approval`,
    expectedAssetVersionInputSchema,
    mcpVersionResponseSchema,
    input,
    signal,
  );
}

function projectBindingUrl(projectId: string, kind: AssetKind): string {
  const parsedProjectId = parseInput(assetIdSchema, projectId);
  const parsedKind = parseInput(assetKindSchema, kind);
  return `${getBackendBaseURL()}/api/projects/${parsedProjectId}/system-${parsedKind}-bindings`;
}

function adminProjectBindingUrl(projectId: string, kind: AssetKind): string {
  const parsedProjectId = parseInput(assetIdSchema, projectId);
  const parsedKind = parseInput(assetKindSchema, kind);
  return `${getBackendBaseURL()}/api/admin/projects/${parsedProjectId}/assets/system-${parsedKind}-bindings`;
}

async function mutateProjectBinding(
  url: string,
  schema: z.ZodType<unknown>,
  input: unknown,
  signal?: AbortSignal,
  responseSchema: z.ZodType<
    SystemBinding,
    z.ZodTypeDef,
    unknown
  > = systemBindingSchema,
): Promise<SystemBinding> {
  const body = parseInput(schema, input);
  const response = await request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, responseSchema);
}

export function enableProjectSystemBinding(
  projectId: string,
  kind: AssetKind,
  input: EnableSystemBindingInput | EnableCurrentSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  return mutateProjectBinding(
    projectBindingUrl(projectId, kind),
    kind === "agent" || kind === "skill"
      ? enableCurrentSystemBindingInputSchema
      : enableSystemBindingInputSchema,
    input,
    signal,
    kind === "agent" || kind === "skill"
      ? currentSystemBindingSchema
      : systemBindingSchema,
  );
}

export function syncCurrentProjectSystemMcpBinding(
  projectId: string,
  assetId: string,
  input: SyncCurrentSystemMcpBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  const parsedProjectId = parseInput(assetIdSchema, projectId);
  const id = parseInput(assetIdSchema, assetId);
  const responseSchema = systemBindingSchema.superRefine((value, context) => {
    if (value.kind !== "mcp") {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "MCP sync response must contain an MCP binding",
        path: ["kind"],
      });
    }
    if (value.project_id !== parsedProjectId) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "MCP sync response must match the requested project",
        path: ["project_id"],
      });
    }
    if (value.asset_id !== id) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "MCP sync response must match the requested asset",
        path: ["asset_id"],
      });
    }
  });
  return mutateProjectBinding(
    `${projectBindingUrl(parsedProjectId, "mcp")}/${id}/sync-current`,
    syncCurrentSystemMcpBindingInputSchema,
    input,
    signal,
    responseSchema,
  );
}

function moveProjectSystemBinding(
  projectId: string,
  kind: "mcp",
  assetId: string,
  action: "upgrade" | "rollback",
  input: MoveSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  const id = parseInput(assetIdSchema, assetId);
  return mutateProjectBinding(
    `${projectBindingUrl(projectId, kind)}/${id}/${action}`,
    moveSystemBindingInputSchema,
    input,
    signal,
  );
}

export function upgradeProjectSystemBinding(
  projectId: string,
  kind: "mcp",
  assetId: string,
  input: MoveSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  return moveProjectSystemBinding(
    projectId,
    kind,
    assetId,
    "upgrade",
    input,
    signal,
  );
}

export function rollbackProjectSystemBinding(
  projectId: string,
  kind: "mcp",
  assetId: string,
  input: MoveSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  return moveProjectSystemBinding(
    projectId,
    kind,
    assetId,
    "rollback",
    input,
    signal,
  );
}

export function disableProjectSystemBinding(
  projectId: string,
  kind: AssetKind,
  assetId: string,
  input: DisableSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  const id = parseInput(assetIdSchema, assetId);
  return mutateProjectBinding(
    `${projectBindingUrl(projectId, kind)}/${id}/disable`,
    disableSystemBindingInputSchema,
    input,
    signal,
    kind === "agent" || kind === "skill"
      ? currentSystemBindingSchema
      : systemBindingSchema,
  );
}

export function enableAdminProjectSystemBinding(
  projectId: string,
  kind: AssetKind,
  input: EnableSystemBindingInput | EnableCurrentSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  return mutateProjectBinding(
    adminProjectBindingUrl(projectId, kind),
    kind === "agent" || kind === "skill"
      ? enableCurrentSystemBindingInputSchema
      : enableSystemBindingInputSchema,
    input,
    signal,
    kind === "agent" || kind === "skill"
      ? currentSystemBindingSchema
      : systemBindingSchema,
  );
}

function moveAdminProjectSystemBinding(
  projectId: string,
  kind: "mcp",
  assetId: string,
  action: "upgrade" | "rollback",
  input: MoveSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  const id = parseInput(assetIdSchema, assetId);
  return mutateProjectBinding(
    `${adminProjectBindingUrl(projectId, kind)}/${id}/${action}`,
    moveSystemBindingInputSchema,
    input,
    signal,
  );
}

export function upgradeAdminProjectSystemBinding(
  projectId: string,
  kind: "mcp",
  assetId: string,
  input: MoveSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  return moveAdminProjectSystemBinding(
    projectId,
    kind,
    assetId,
    "upgrade",
    input,
    signal,
  );
}

export function rollbackAdminProjectSystemBinding(
  projectId: string,
  kind: "mcp",
  assetId: string,
  input: MoveSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  return moveAdminProjectSystemBinding(
    projectId,
    kind,
    assetId,
    "rollback",
    input,
    signal,
  );
}

export function disableAdminProjectSystemBinding(
  projectId: string,
  kind: AssetKind,
  assetId: string,
  input: DisableSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  const id = parseInput(assetIdSchema, assetId);
  return mutateProjectBinding(
    `${adminProjectBindingUrl(projectId, kind)}/${id}/disable`,
    disableSystemBindingInputSchema,
    input,
    signal,
    kind === "agent" || kind === "skill"
      ? currentSystemBindingSchema
      : systemBindingSchema,
  );
}
