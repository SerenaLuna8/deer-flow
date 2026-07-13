import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  adminAssetListSchema,
  adminCredentialListSchema,
  assetIdSchema,
  assetListKindSchema,
  assetMutationResponseSchema,
  createAssetInputSchema,
  expectedAssetVersionInputSchema,
  projectAssetListSchema,
  projectCredentialListSchema,
  type AdminAssetList,
  type AdminCredentialList,
  type AssetListKind,
  type AssetMutationResponse,
  type CreateAssetInput,
  type ExpectedAssetVersionInput,
  type ProjectAssetList,
  type ProjectCredentialList,
} from "./types";

const serverErrorCodeSchema = z.enum([
  "asset_not_found",
  "asset_forbidden",
  "asset_conflict",
  "asset_validation_failed",
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
  "ASSET_STORAGE_UNAVAILABLE",
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

function adminAssetUrl(kind: AssetListKind): string {
  return `${getBackendBaseURL()}/api/admin/assets/${parseInput(
    assetListKindSchema,
    kind,
  )}`;
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

export async function createProjectAsset(
  projectId: string,
  kind: Exclude<AssetListKind, "credentials">,
  input: CreateAssetInput,
  signal?: AbortSignal,
): Promise<AssetMutationResponse> {
  const body = parseInput(createAssetInputSchema, input);
  const response = await request(projectAssetUrl(projectId, kind), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, assetMutationResponseSchema);
}

export async function createAdminAsset(
  kind: Exclude<AssetListKind, "credentials">,
  input: CreateAssetInput,
  signal?: AbortSignal,
): Promise<AssetMutationResponse> {
  const body = parseInput(createAssetInputSchema, input);
  const response = await request(adminAssetUrl(kind), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return parseResponse(response, assetMutationResponseSchema);
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

export function changeProjectAssetStatus(
  projectId: string,
  kind: Exclude<AssetListKind, "credentials">,
  assetId: string,
  action: "archive" | "suspend",
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
) {
  const id = parseInput(assetIdSchema, assetId);
  return changeAssetStatus(
    `${projectAssetUrl(projectId, kind)}/${id}/${action}`,
    input,
    signal,
  );
}

export function changeAdminAssetStatus(
  kind: Exclude<AssetListKind, "credentials">,
  assetId: string,
  action: "archive" | "suspend",
  input: ExpectedAssetVersionInput,
  signal?: AbortSignal,
) {
  const id = parseInput(assetIdSchema, assetId);
  return changeAssetStatus(
    `${adminAssetUrl(kind)}/${id}/${action}`,
    input,
    signal,
  );
}
