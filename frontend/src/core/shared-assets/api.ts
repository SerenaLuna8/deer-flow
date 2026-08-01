import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  adminAssetListSchema,
  adminCredentialListSchema,
  agentInstructionsInputSchema,
  agentVersionHistoryResponseSchema,
  agentVersionResponseSchema,
  approveMcpInputSchema,
  assetIdSchema,
  assetKindSchema,
  assetListKindSchema,
  assetMutationResponseSchema,
  createAssetInputSchema,
  createCredentialInputSchema,
  configureSystemMcpCredentialGrantsInputSchema,
  credentialGrantMigrationResponseSchema,
  credentialMutationResponseSchema,
  credentialRotationStatusSchema,
  credentialVersionHistoryResponseSchema,
  credentialVersionResponseSchema,
  disableSystemBindingInputSchema,
  deleteCredentialInputSchema,
  enableSystemBindingInputSchema,
  expectedAssetVersionInputSchema,
  moveSystemBindingInputSchema,
  mcpVersionHistoryResponseSchema,
  mcpVersionInputSchema,
  mcpVersionResponseSchema,
  migrateCredentialGrantsInputSchema,
  projectAssetListSchema,
  projectCredentialListSchema,
  projectDefaultAgentInputSchema,
  projectDefaultAgentSchema,
  projectSkillImportResponseSchema,
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
  systemBindingSchema,
  type AdminAssetList,
  type AdminCredentialList,
  type AgentInstructionsInput,
  type AgentVersionResponse,
  type ApproveMcpInput,
  type AssetKind,
  type AssetListKind,
  type AssetMutationResponse,
  type CreateAssetInput,
  type CreateCredentialInput,
  type ConfigureSystemMcpCredentialGrantsInput,
  type CredentialGrantMigrationResponse,
  type CredentialMutationResponse,
  type CredentialRotationStatus,
  type DeleteCredentialInput,
  type DisableSystemBindingInput,
  type EnableSystemBindingInput,
  type ExpectedAssetVersionInput,
  type MoveSystemBindingInput,
  type McpVersionInput,
  type MigrateCredentialGrantsInput,
  type ProjectAssetList,
  type ProjectCredentialList,
  type ProjectDefaultAgent,
  type ProjectDefaultAgentInput,
  type ProjectSkillImportResponse,
  type ReplaceCredentialInput,
  type RevokeCredentialInput,
  type SkillCredentialBindingsInput,
  type SkillCredentialBindingsResponse,
  type SkillVersionInput,
  type SkillFileForkInput,
  type SkillVersionFileContentResponse,
  type SystemBinding,
  type VersionHistoryResponse,
  type VersionResponse,
} from "./types";

type MutableAssetListKind = Exclude<AssetListKind, "credentials">;
type VersionedAssetListKind = Exclude<MutableAssetListKind, "agents">;
export type ProjectAssetStatusAction<Kind extends MutableAssetListKind> =
  Kind extends "skills"
    ? "activate" | "suspend"
    : Kind extends "agents"
      ? "activate" | "suspend"
      : "archive" | "suspend";

const serverErrorCodeSchema = z.enum([
  "asset_not_found",
  "asset_forbidden",
  "asset_conflict",
  "asset_validation_failed",
  "asset_storage_quota_exceeded",
  "asset_storage_unavailable",
]);

const errorEnvelopeSchema = z
  .object({
    detail: z
      .object({
        code: serverErrorCodeSchema,
        message: z.string().min(1),
        request_id: z.string().min(1).optional(),
      })
      .strict(),
  })
  .strict();

const SAFE_SERVER_ERRORS = {
  asset_not_found: ["ASSET_NOT_FOUND", "Asset not found"],
  asset_forbidden: ["ASSET_FORBIDDEN", "Asset capability required"],
  asset_conflict: ["ASSET_CONFLICT", "Asset state conflict"],
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
} as const;

export const SHARED_ASSET_ERROR_CODES = [
  "ASSET_NOT_FOUND",
  "ASSET_FORBIDDEN",
  "ASSET_CONFLICT",
  "ASSET_VALIDATION_FAILED",
  "ASSET_STORAGE_QUOTA_EXCEEDED",
  "ASSET_STORAGE_UNAVAILABLE",
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

  constructor(status: number, code: SharedAssetErrorCode, message: string) {
    super(message);
    this.name = "SharedAssetApiError";
    this.status = status;
    this.code = code;
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
  throw new SharedAssetApiError(response.status, code, message);
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

export async function getAdminCredentialRotationStatus(
  signal?: AbortSignal,
): Promise<CredentialRotationStatus> {
  const response = await request(
    `${adminAssetUrl("credentials")}/rotation-status`,
    { signal },
  );
  return parseResponse(response, credentialRotationStatusSchema);
}

async function createAsset(
  url: string,
  input: CreateAssetInput,
  signal?: AbortSignal,
): Promise<AssetMutationResponse> {
  const body = parseInput(createAssetInputSchema, input);
  const response = await request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, assetMutationResponseSchema);
}

export async function createProjectAsset(
  projectId: string,
  kind: Exclude<AssetListKind, "credentials">,
  input: CreateAssetInput,
  signal?: AbortSignal,
): Promise<AssetMutationResponse> {
  return await createAsset(projectAssetUrl(projectId, kind), input, signal);
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

export async function createAdminProjectAsset(
  projectId: string,
  kind: Exclude<AssetListKind, "credentials">,
  input: CreateAssetInput,
  signal?: AbortSignal,
): Promise<AssetMutationResponse> {
  return await createAsset(
    adminProjectAssetUrl(projectId, kind),
    input,
    signal,
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
  const validAction =
    kind === "skills"
      ? action === "activate" || action === "suspend"
      : kind === "agents"
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
  action: ProjectAssetStatusAction<Kind>,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
) {
  const validAction =
    kind === "skills"
      ? action === "activate" || action === "suspend"
      : kind === "agents"
        ? action === "suspend"
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
  signal?: AbortSignal,
): Promise<SkillCredentialBindingsResponse> {
  const skill = parseInput(assetIdSchema, skillId);
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/${skill}/credential-bindings`,
    { signal },
  );
  return parseResponse(response, skillCredentialBindingsResponseSchema);
}

export async function updateProjectSkillCredentialBindings(
  projectId: string,
  skillId: string,
  input: SkillCredentialBindingsInput,
  signal?: AbortSignal,
): Promise<SkillCredentialBindingsResponse> {
  const skill = parseInput(assetIdSchema, skillId);
  const body = parseInput(skillCredentialBindingsInputSchema, input);
  const response = await request(
    `${projectAssetUrl(projectId, "skills")}/${skill}/credential-bindings`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return parseResponse(response, skillCredentialBindingsResponseSchema);
}

async function postVersionMutation<T>(
  url: string,
  inputSchema: z.ZodType<T>,
  responseSchema: z.ZodType<VersionResponse>,
  input: unknown,
  signal?: AbortSignal,
): Promise<VersionResponse> {
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
  kind: VersionedAssetListKind,
  assetId: string,
  versionId: string,
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return postVersionMutation(
    `${projectAssetUrl(projectId, kind)}/${asset}/versions/${version}/publish`,
    expectedAssetVersionInputSchema,
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
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const asset = parseInput(assetIdSchema, assetId);
  const version = parseInput(assetIdSchema, versionId);
  return postVersionMutation(
    `${adminProjectAssetUrl(projectId, kind)}/${asset}/versions/${version}/publish`,
    expectedAssetVersionInputSchema,
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
): Promise<VersionResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return postVersionMutation(
    `${projectAssetUrl(projectId, "credentials")}/${id}/replace`,
    replaceCredentialInputSchema,
    credentialVersionResponseSchema,
    input,
    signal,
  );
}

export function replaceAdminCredential(
  credentialId: string,
  input: ReplaceCredentialInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return postVersionMutation(
    `${adminAssetUrl("credentials")}/${id}/replace`,
    replaceCredentialInputSchema,
    credentialVersionResponseSchema,
    input,
    signal,
  );
}

export function replaceAdminProjectCredential(
  projectId: string,
  credentialId: string,
  input: ReplaceCredentialInput,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  const id = parseInput(assetIdSchema, credentialId);
  return postVersionMutation(
    `${adminProjectAssetUrl(projectId, "credentials")}/${id}/replace`,
    replaceCredentialInputSchema,
    credentialVersionResponseSchema,
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
): Promise<SystemBinding> {
  const body = parseInput(schema, input);
  const response = await request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, systemBindingSchema);
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
