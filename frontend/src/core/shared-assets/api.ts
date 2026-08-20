import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  skillFrontmatterParseInputSchema,
  skillFrontmatterParseResponseSchema,
  skillFrontmatterDiagnosticSchema,
  skillFrontmatterPatchInputSchema,
  skillFrontmatterPatchResponseSchema,
  skillPublishPlanResponseSchema,
  type SkillFrontmatterDiagnostic,
  type SkillFrontmatterParseInput,
  type SkillFrontmatterParseResponse,
  type SkillFrontmatterPatchInput,
  type SkillFrontmatterPatchResponse,
  type SkillPublishPlanResponse,
} from "./skill-secret-declarations";
import {
  adminAssetListSchema,
  adminCredentialListSchema,
  agentCapabilityBindingsInputSchema,
  agentCreateResponseSchema,
  agentInstructionsInputSchema,
  agentRuntimeAssessmentsInputSchema,
  agentRuntimeAssessmentsResponseSchema,
  agentVersionHistoryResponseSchema,
  agentVersionResponseSchema,
  approveMcpInputSchema,
  assetIdSchema,
  assetKindSchema,
  assetListKindSchema,
  assetMutationResponseSchema,
  configuredMcpResponseSchema,
  createAgentInputSchema,
  createConfiguredMcpInputSchema,
  createCredentialInputSchema,
  configureSystemMcpCredentialGrantsInputSchema,
  credentialGrantMigrationResponseSchema,
  credentialMigrationStatusResponseSchema,
  credentialMutationResponseSchema,
  credentialReplacementResponseSchema,
  credentialVersionHistoryResponseSchema,
  disableSystemBindingInputSchema,
  deleteCredentialInputSchema,
  enableSystemBindingInputSchema,
  expectedAssetVersionInputSchema,
  moveSystemBindingInputSchema,
  mcpToolDiscoveryAttemptResponseSchema,
  mcpToolInventoryResponseSchema,
  mcpVersionHistoryResponseSchema,
  mcpVersionInputSchema,
  mcpVersionResponseSchema,
  migrateCredentialGrantsInputSchema,
  projectAssetListSchema,
  projectCredentialListSchema,
  projectDefaultAgentInputSchema,
  projectDefaultAgentSchema,
  projectMcpEditableConfigurationResponseSchema,
  projectSkillImportResponseSchema,
  publishAssetVersionInputSchema,
  skillPublishAssetVersionInputSchema,
  replaceCredentialInputSchema,
  revokeCredentialInputSchema,
  skillCredentialBindingsInputSchema,
  skillCredentialBindingsResponseSchema,
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
  type AdminCredentialList,
  type AgentCapabilityBindingsInput,
  type AgentCreateResponse,
  type AgentInstructionsInput,
  type AgentRuntimeAssessmentsResponse,
  type AgentVersionResponse,
  type ApproveMcpInput,
  type AssetKind,
  type AssetListKind,
  type AssetMutationResponse,
  type ConfiguredMcpResponse,
  type CreateAgentInput,
  type CreateConfiguredMcpInput,
  type CreateCredentialInput,
  type CredentialMigrationStatusResponse,
  type ConfigureSystemMcpCredentialGrantsInput,
  type CredentialGrantMigrationResponse,
  type CredentialMutationResponse,
  type CredentialReplacementResponse,
  type DeleteCredentialInput,
  type DisableSystemBindingInput,
  type EnableSystemBindingInput,
  type ExpectedAssetVersionInput,
  type MoveSystemBindingInput,
  type PublishAssetVersionInput,
  type SkillPublishAssetVersionInput,
  type McpToolDiscoveryAttemptResponse,
  type McpVersionInput,
  type McpToolInventoryResponse,
  type MigrateCredentialGrantsInput,
  type ProjectAssetList,
  type ProjectAssetStatusAction,
  type ProjectCredentialList,
  type ProjectDefaultAgent,
  type ProjectDefaultAgentInput,
  type ProjectMcpEditableConfigurationResponse,
  type ProjectSkillImportResponse,
  type ReplaceCredentialInput,
  type RevokeCredentialInput,
  type SkillCredentialBindingsInput,
  type SkillCredentialBindingsResponse,
  type SkillVersionInput,
  type SkillFileForkInput,
  type SkillVersionFileContentResponse,
  type SystemBinding,
  type SyncCurrentSystemMcpBindingInput,
  type UpdateConfiguredMcpInput,
  type VersionHistoryResponse,
  type VersionResponse,
} from "./types";

type MutableAssetListKind = Exclude<AssetListKind, "credentials">;
type VersionedAssetListKind = MutableAssetListKind;

export type SkillDistributionDownload = {
  filename: string;
  content: Blob;
};

const SKILL_DISTRIBUTION_FILENAME_PATTERN =
  /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?-v[1-9][0-9]*\.zip$/u;

const serverErrorCodeSchema = z.enum([
  "asset_not_found",
  "asset_forbidden",
  "asset_conflict",
  "ASSET_IN_USE",
  "asset_validation_failed",
  "asset_storage_quota_exceeded",
  "asset_storage_unavailable",
  "SKILL_ARCHIVE_LIMIT_EXCEEDED",
  "SKILL_PUBLISH_BASE_STALE",
  "SKILL_RUNTIME_NAME_CONFLICT",
  "SKILL_SECRET_DECLARATION_INVALID",
  "SKILL_FRONTMATTER_SOURCE_STALE",
  "SKILL_CREDENTIAL_BINDINGS_INCOMPLETE",
  "SKILL_CREDENTIAL_BINDING_INVALID",
  "SKILL_CREDENTIAL_SELECTION_STALE",
]);

const errorEnvelopeSchema = z
  .object({
    detail: z
      .object({
        code: serverErrorCodeSchema,
        message: z.string().min(1),
        request_id: z.string().min(1).optional(),
        diagnostics: z.array(skillFrontmatterDiagnosticSchema).optional(),
      })
      .strict(),
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
  SKILL_PUBLISH_BASE_STALE: [
    "SKILL_PUBLISH_BASE_STALE",
    "The version being published was not based on the live published version",
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
  SKILL_CREDENTIAL_BINDINGS_INCOMPLETE: [
    "SKILL_CREDENTIAL_BINDINGS_INCOMPLETE",
    "Required Skill Credential bindings are incomplete",
  ],
  SKILL_CREDENTIAL_BINDING_INVALID: [
    "SKILL_CREDENTIAL_BINDING_INVALID",
    "Skill Credential binding is invalid",
  ],
  SKILL_CREDENTIAL_SELECTION_STALE: [
    "SKILL_CREDENTIAL_SELECTION_STALE",
    "Skill Credential selection is stale",
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
  "SKILL_PUBLISH_BASE_STALE",
  "SKILL_RUNTIME_NAME_CONFLICT",
  "SKILL_SECRET_DECLARATION_INVALID",
  "SKILL_FRONTMATTER_SOURCE_STALE",
  "SKILL_CREDENTIAL_BINDINGS_INCOMPLETE",
  "SKILL_CREDENTIAL_BINDING_INVALID",
  "SKILL_CREDENTIAL_SELECTION_STALE",
  "ASSET_UPLOAD_TOO_LARGE",
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

  constructor(
    status: number,
    code: SharedAssetErrorCode,
    message: string,
    diagnostics?: readonly SkillFrontmatterDiagnostic[],
  ) {
    super(message);
    this.name = "SharedAssetApiError";
    this.status = status;
    this.code = code;
    this.diagnostics = diagnostics;
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
  throw new SharedAssetApiError(
    response.status,
    code,
    message,
    parsed.data.detail.diagnostics,
  );
}

async function parseResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
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

function systemCatalogUrl(kind: Exclude<AssetListKind, "credentials">): string {
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
  if (kind === "mcp-servers") return mcpVersionHistoryResponseSchema;
  return credentialVersionHistoryResponseSchema;
}

function publishVersionSchema(
  kind: VersionedAssetListKind,
): z.ZodType<VersionResponse> {
  if (kind === "agents") return agentVersionResponseSchema;
  if (kind === "skills") return skillVersionResponseSchema;
  return mcpVersionResponseSchema;
}

export function listProjectAssets(
  projectId: string,
  kind: "credentials",
  signal?: AbortSignal,
): Promise<ProjectCredentialList>;
export function listProjectAssets(
  projectId: string,
  kind: Exclude<AssetListKind, "credentials">,
  signal?: AbortSignal,
): Promise<ProjectAssetList>;
export async function listProjectAssets(
  projectId: string,
  kind: AssetListKind,
  signal?: AbortSignal,
): Promise<ProjectAssetList | ProjectCredentialList> {
  const response = await request(projectAssetUrl(projectId, kind), { signal });
  if (kind === "credentials") {
    return parseResponse(response, projectCredentialListSchema);
  }
  return parseResponse(response, projectAssetListSchema);
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

export function listAdminAssets(
  kind: "credentials",
  signal?: AbortSignal,
): Promise<AdminCredentialList>;
export function listAdminAssets(
  kind: Exclude<AssetListKind, "credentials">,
  signal?: AbortSignal,
): Promise<AdminAssetList>;
export async function listAdminAssets(
  kind: AssetListKind,
  signal?: AbortSignal,
): Promise<AdminAssetList | AdminCredentialList> {
  const response = await request(adminAssetUrl(kind), { signal });
  if (kind === "credentials") {
    return parseResponse(response, adminCredentialListSchema);
  }
  return parseResponse(response, adminAssetListSchema);
}

export function listAdminProjectAssets(
  projectId: string,
  kind: "credentials",
  signal?: AbortSignal,
): Promise<ProjectCredentialList>;
export function listAdminProjectAssets(
  projectId: string,
  kind: Exclude<AssetListKind, "credentials">,
  signal?: AbortSignal,
): Promise<ProjectAssetList>;
export async function listAdminProjectAssets(
  projectId: string,
  kind: AssetListKind,
  signal?: AbortSignal,
): Promise<ProjectAssetList | ProjectCredentialList> {
  const response = await request(adminProjectAssetUrl(projectId, kind), {
    signal,
  });
  return kind === "credentials"
    ? parseResponse(response, projectCredentialListSchema)
    : parseResponse(response, projectAssetListSchema);
}

export async function listSystemAssetCatalog(
  kind: Exclude<AssetListKind, "credentials">,
  signal?: AbortSignal,
): Promise<AdminAssetList> {
  const response = await request(systemCatalogUrl(kind), { signal });
  return parseResponse(response, adminAssetListSchema);
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
  credentialSlotNames,
}: {
  projectId: string;
  assetId?: string;
  expectedAssetVersion?: number;
  credentialSlotNames: readonly string[];
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

    const responseSlotNames = value.version.credential_slots.map(
      (slot) => slot.name,
    );
    if (
      responseSlotNames.length !== credentialSlotNames.length ||
      credentialSlotNames.some((name) => !responseSlotNames.includes(name))
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Configured MCP response Credential slots must match the request",
        path: ["version", "credential_slots"],
      });
    }

    const expectedWorkflowStatus =
      credentialSlotNames.length === 0 ? "published" : "pending_approval";
    if (value.version.workflow_status !== expectedWorkflowStatus) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Configured MCP response workflow must match the requested Credential slots",
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
      credentialSlotNames: (body.credential_slots ?? []).map(
        (slot) => slot.name,
      ),
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
      credentialSlotNames: (body.credential_slots ?? []).map(
        (slot) => slot.name,
      ),
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

export async function restoreProjectAgentVersion(
  projectId: string,
  assetId: string,
  versionId: string,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<AgentVersionResponse> {
  const id = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  const body = parseInput(expectedAssetVersionInputSchema, input);
  const response = await request(
    `${projectAssetUrl(projectId, "agents")}/${id}/versions/${version}/restore`,
    {
      method: "POST",
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
  signal?: AbortSignal,
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
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/import`,
    {
      method: "POST",
      body,
      signal,
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

export async function getProjectSkillPublishPlan(
  projectId: string,
  skillId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<SkillPublishPlanResponse> {
  const skill = parseInput(assetIdSchema, skillId);
  const version = parseInput(assetIdSchema, versionId);
  return parseResponse(
    await request(
      `${projectAssetUrl(projectId, "skills")}/${skill}/versions/${version}/publish-plan`,
      { signal },
    ),
    skillPublishPlanResponseSchema.superRefine((value, context) => {
      if (value.skill_id !== skill) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill publish plan belongs to another Skill",
          path: ["skill_id"],
        });
      }
      if (value.skill_version_id !== version) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill publish plan belongs to another version",
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
    publishVersionSchema(kind),
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
    publishVersionSchema(kind),
    input,
    signal,
  );
}

async function createCredential(
  url: string,
  input: CreateCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialMutationResponse> {
  const body = parseInput(createCredentialInputSchema, input);
  const response = await request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, credentialMutationResponseSchema);
}

export function createProjectCredential(
  projectId: string,
  input: CreateCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialMutationResponse> {
  return createCredential(
    projectAssetUrl(projectId, "credentials"),
    input,
    signal,
  );
}

export function createAdminCredential(
  input: CreateCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialMutationResponse> {
  return createCredential(adminAssetUrl("credentials"), input, signal);
}

export function createAdminProjectCredential(
  projectId: string,
  input: CreateCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialMutationResponse> {
  return createCredential(
    adminProjectAssetUrl(projectId, "credentials"),
    input,
    signal,
  );
}

async function changeAssetStatus(
  url: string,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<AssetMutationResponse> {
  const body = parseInput(expectedAssetVersionInputSchema, input);
  const response = await request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, assetMutationResponseSchema);
}

export function changeProjectAssetStatus<Kind extends MutableAssetListKind>(
  projectId: string,
  kind: Kind,
  assetId: string,
  action: ProjectAssetStatusAction<Kind>,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
) {
  const validAction = action === "activate" || action === "suspend";
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
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
) {
  const validAction =
    kind === "skills" || kind === "agents"
      ? action === "activate" || action === "suspend"
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
    signal,
  );
}

export async function deleteProjectSkill(
  projectId: string,
  assetId: string,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<void> {
  const id = parseInput(assetIdSchema, assetId);
  const body = parseInput(expectedAssetVersionInputSchema, input);
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
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<void> {
  const id = parseInput(assetIdSchema, assetId);
  const body = parseInput(expectedAssetVersionInputSchema, input);
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

async function deleteCredential(
  url: string,
  input: DeleteCredentialInput,
  signal?: AbortSignal,
): Promise<void> {
  const body = parseInput(deleteCredentialInputSchema, input);
  const response = await request(url, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) await throwResponseError(response);
}

export function deleteProjectCredential(
  projectId: string,
  credentialId: string,
  input: DeleteCredentialInput,
  signal?: AbortSignal,
): Promise<void> {
  const id = parseInput(assetIdSchema, credentialId);
  return deleteCredential(
    `${projectAssetUrl(projectId, "credentials")}/${id}`,
    input,
    signal,
  );
}

export function deleteAdminCredential(
  credentialId: string,
  input: DeleteCredentialInput,
  signal?: AbortSignal,
): Promise<void> {
  const id = parseInput(assetIdSchema, credentialId);
  return deleteCredential(
    `${adminAssetUrl("credentials")}/${id}`,
    input,
    signal,
  );
}

export function deleteAdminProjectCredential(
  projectId: string,
  credentialId: string,
  input: DeleteCredentialInput,
  signal?: AbortSignal,
): Promise<void> {
  const id = parseInput(assetIdSchema, credentialId);
  return deleteCredential(
    `${adminProjectAssetUrl(projectId, "credentials")}/${id}`,
    input,
    signal,
  );
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

export async function getProjectSkillCredentialBindings(
  projectId: string,
  skillId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<SkillCredentialBindingsResponse> {
  const skill = parseInput(assetIdSchema, skillId);
  const version = parseInput(assetIdSchema, versionId);
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/${skill}/versions/${version}/credential-bindings`,
    { signal },
  );
  return parseResponse(
    response,
    skillCredentialBindingsResponseSchema.superRefine((value, context) => {
      if (value.skill_id !== skill) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill Credential bindings belong to another Skill",
          path: ["skill_id"],
        });
      }
      if (value.skill_version_id !== version) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill Credential bindings belong to another version",
          path: ["skill_version_id"],
        });
      }
    }),
  );
}

export async function updateProjectSkillCredentialBindings(
  projectId: string,
  skillId: string,
  versionId: string,
  input: SkillCredentialBindingsInput,
  signal?: AbortSignal,
): Promise<SkillCredentialBindingsResponse> {
  const skill = parseInput(assetIdSchema, skillId);
  const version = parseInput(assetIdSchema, versionId);
  const body = parseInput(skillCredentialBindingsInputSchema, input);
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/${skill}/versions/${version}/credential-bindings`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return parseResponse(
    response,
    skillCredentialBindingsResponseSchema.superRefine((value, context) => {
      if (value.skill_id !== skill) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill Credential bindings belong to another Skill",
          path: ["skill_id"],
        });
      }
      if (value.skill_version_id !== version) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Skill Credential bindings belong to another version",
          path: ["skill_version_id"],
        });
      }
    }),
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

export function publishProjectAssetVersion(
  projectId: string,
  kind: "skills",
  assetId: string,
  versionId: string,
  input: SkillPublishAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse>;
export function publishProjectAssetVersion(
  projectId: string,
  kind: Exclude<VersionedAssetListKind, "skills">,
  assetId: string,
  versionId: string,
  input: PublishAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse>;
export function publishProjectAssetVersion(
  projectId: string,
  kind: VersionedAssetListKind,
  assetId: string,
  versionId: string,
  input: PublishAssetVersionInput | SkillPublishAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return postVersionMutation(
    `${projectAssetUrl(projectId, kind)}/${asset}/versions/${version}/publish`,
    kind === "skills"
      ? skillPublishAssetVersionInputSchema
      : publishAssetVersionInputSchema,
    publishVersionSchema(kind),
    input,
    signal,
  );
}

export function publishAdminProjectAssetVersion(
  projectId: string,
  kind: VersionedAssetListKind,
  assetId: string,
  versionId: string,
  input: PublishAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return postVersionMutation(
    `${adminProjectAssetUrl(projectId, kind)}/${asset}/versions/${version}/publish`,
    publishAssetVersionInputSchema,
    publishVersionSchema(kind),
    input,
    signal,
  );
}

export function replaceProjectCredential(
  projectId: string,
  credentialId: string,
  input: ReplaceCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialReplacementResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return postVersionMutation(
    `${projectAssetUrl(projectId, "credentials")}/${id}/replace`,
    replaceCredentialInputSchema,
    credentialReplacementResponseSchema,
    input,
    signal,
  );
}

export function replaceAdminCredential(
  credentialId: string,
  input: ReplaceCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialReplacementResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return postVersionMutation(
    `${adminAssetUrl("credentials")}/${id}/replace`,
    replaceCredentialInputSchema,
    credentialReplacementResponseSchema,
    input,
    signal,
  );
}

export function replaceAdminProjectCredential(
  projectId: string,
  credentialId: string,
  input: ReplaceCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialReplacementResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return postVersionMutation(
    `${adminProjectAssetUrl(projectId, "credentials")}/${id}/replace`,
    replaceCredentialInputSchema,
    credentialReplacementResponseSchema,
    input,
    signal,
  );
}

async function revokeCredential(
  url: string,
  input: RevokeCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialMutationResponse> {
  const body = parseInput(revokeCredentialInputSchema, input);
  const response = await request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, credentialMutationResponseSchema);
}

export function revokeProjectCredential(
  projectId: string,
  credentialId: string,
  input: RevokeCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialMutationResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return revokeCredential(
    `${projectAssetUrl(projectId, "credentials")}/${id}/revoke`,
    input,
    signal,
  );
}

export function revokeAdminCredential(
  credentialId: string,
  input: RevokeCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialMutationResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return revokeCredential(
    `${adminAssetUrl("credentials")}/${id}/revoke`,
    input,
    signal,
  );
}

export function revokeAdminProjectCredential(
  projectId: string,
  credentialId: string,
  input: RevokeCredentialInput,
  signal?: AbortSignal,
): Promise<CredentialMutationResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return revokeCredential(
    `${adminProjectAssetUrl(projectId, "credentials")}/${id}/revoke`,
    input,
    signal,
  );
}

async function migrateCredentialGrants(
  url: string,
  input: MigrateCredentialGrantsInput,
  signal?: AbortSignal,
): Promise<CredentialGrantMigrationResponse> {
  const body = parseInput(migrateCredentialGrantsInputSchema, input);
  const response = await request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, credentialGrantMigrationResponseSchema);
}

async function getCredentialMigrationStatus(
  url: string,
  signal?: AbortSignal,
): Promise<CredentialMigrationStatusResponse> {
  const response = await request(url, { signal });
  return parseResponse(response, credentialMigrationStatusResponseSchema);
}

export function getAdminCredentialMigrationStatus(
  credentialId: string,
  signal?: AbortSignal,
): Promise<CredentialMigrationStatusResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return getCredentialMigrationStatus(
    `${adminAssetUrl("credentials")}/${id}/migration-status`,
    signal,
  );
}

export function getAdminProjectCredentialMigrationStatus(
  projectId: string,
  credentialId: string,
  signal?: AbortSignal,
): Promise<CredentialMigrationStatusResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return getCredentialMigrationStatus(
    `${adminProjectAssetUrl(projectId, "credentials")}/${id}/migration-status`,
    signal,
  );
}

export function getProjectCredentialMigrationStatus(
  projectId: string,
  credentialId: string,
  signal?: AbortSignal,
): Promise<CredentialMigrationStatusResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return getCredentialMigrationStatus(
    `${projectAssetUrl(projectId, "credentials")}/${id}/migration-status`,
    signal,
  );
}

export function migrateProjectCredentialGrants(
  projectId: string,
  credentialId: string,
  input: MigrateCredentialGrantsInput,
  signal?: AbortSignal,
): Promise<CredentialGrantMigrationResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return migrateCredentialGrants(
    `${projectAssetUrl(projectId, "credentials")}/${id}/migrate-grants`,
    input,
    signal,
  );
}

export function migrateAdminCredentialGrants(
  credentialId: string,
  input: MigrateCredentialGrantsInput,
  signal?: AbortSignal,
): Promise<CredentialGrantMigrationResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return migrateCredentialGrants(
    `${adminAssetUrl("credentials")}/${id}/migrate-grants`,
    input,
    signal,
  );
}

export function migrateAdminProjectCredentialGrants(
  projectId: string,
  credentialId: string,
  input: MigrateCredentialGrantsInput,
  signal?: AbortSignal,
): Promise<CredentialGrantMigrationResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return migrateCredentialGrants(
    `${adminProjectAssetUrl(projectId, "credentials")}/${id}/migrate-grants`,
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

export function approveProjectMcpVersion(
  projectId: string,
  assetId: string,
  versionId: string,
  input: ApproveMcpInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  return postVersionMutation(
    `${projectMcpVersionUrl(projectId, assetId, versionId)}/approve`,
    approveMcpInputSchema,
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

export function approveAdminProjectMcpVersion(
  projectId: string,
  assetId: string,
  versionId: string,
  input: ApproveMcpInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  return postVersionMutation(
    `${adminProjectMcpVersionUrl(projectId, assetId, versionId)}/approve`,
    approveMcpInputSchema,
    mcpVersionResponseSchema,
    input,
    signal,
  );
}

export function configureAdminMcpCredentialGrants(
  assetId: string,
  versionId: string,
  input: ConfigureSystemMcpCredentialGrantsInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return postVersionMutation(
    `${adminAssetUrl("mcp-servers")}/${asset}/versions/${version}/credential-grants`,
    configureSystemMcpCredentialGrantsInputSchema,
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

async function mutateProjectBinding<T>(
  url: string,
  schema: z.ZodType<T>,
  input: unknown,
  signal?: AbortSignal,
  responseSchema: z.ZodType<SystemBinding> = systemBindingSchema,
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
  input: EnableSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  return mutateProjectBinding(
    projectBindingUrl(projectId, kind),
    enableSystemBindingInputSchema,
    input,
    signal,
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
  kind: AssetKind,
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
  kind: AssetKind,
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
  kind: AssetKind,
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
  );
}

export function enableAdminProjectSystemBinding(
  projectId: string,
  kind: AssetKind,
  input: EnableSystemBindingInput,
  signal?: AbortSignal,
): Promise<SystemBinding> {
  return mutateProjectBinding(
    adminProjectBindingUrl(projectId, kind),
    enableSystemBindingInputSchema,
    input,
    signal,
  );
}

function moveAdminProjectSystemBinding(
  projectId: string,
  kind: AssetKind,
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
  kind: AssetKind,
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
  kind: AssetKind,
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
  );
}
