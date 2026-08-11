import { z, type ZodType } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";
import {
  projectClientScopeSchema,
  type ProjectClientScope,
} from "@/core/private-work/types";

import { ProjectWorkflowApiError } from "./api";
import {
  workflowCredentialGrantMutationRequestV1Schema,
  workflowCredentialGrantResponseV1Schema,
  workflowDefinitionArchiveRequestV1Schema,
  workflowDefinitionCreateRequestV1Schema,
  workflowDefinitionListQueryV1Schema,
  workflowDefinitionPageV1Schema,
  workflowDefinitionResponseV1Schema,
  workflowDraftGrantIntentDeleteResponseV1Schema,
  workflowDraftGrantIntentResponseV1Schema,
  workflowDraftResponseV1Schema,
  workflowDraftSaveRequestV1Schema,
  workflowDraftValidateRequestV1Schema,
  workflowDraftValidationResponseV1Schema,
  workflowPublishRequestV1Schema,
  workflowPublishResponseV1Schema,
  workflowVersionListQueryV1Schema,
  workflowVersionPageV1Schema,
  workflowVersionResponseV1Schema,
  type WorkflowCredentialGrantMutationRequestV1,
  type WorkflowCredentialGrantResponseV1,
  type WorkflowDefinitionArchiveRequestV1,
  type WorkflowDefinitionCreateRequestV1,
  type WorkflowDefinitionListQueryV1,
  type WorkflowDefinitionPageV1,
  type WorkflowDefinitionResponseV1,
  type WorkflowDraftGrantIntentDeleteResponseV1,
  type WorkflowDraftGrantIntentResponseV1,
  type WorkflowDraftResponseV1,
  type WorkflowDraftSaveRequestV1,
  type WorkflowDraftValidateRequestV1,
  type WorkflowDraftValidationResponseV1,
  type WorkflowPublishRequestV1,
  type WorkflowPublishResponseV1,
  type WorkflowVersionListQueryV1,
  type WorkflowVersionPageV1,
  type WorkflowVersionResponseV1,
} from "./definition-contracts";

const canonicalUuidSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const slotIdPathSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$/);
const idempotencyKeySchema = z
  .string()
  .min(1)
  .max(255)
  .regex(/^[!-~]+$/);
const requestIdSchema = z
  .string()
  .min(1)
  .max(512)
  .regex(/^[\x20-\x7e]+$/);

const workflowDefinitionServerErrorCodeSchema = z.enum([
  "WORKFLOW_NOT_FOUND",
  "WORKFLOW_FORBIDDEN",
  "WORKFLOW_DRAFT_CONFLICT",
  "WORKFLOW_DRAFT_INVALID",
  "WORKFLOW_VERSION_NOT_EXECUTABLE",
  "WORKFLOW_RUN_CONFLICT",
  "WORKFLOW_RUN_NOT_RESUMABLE",
  "WORKFLOW_RUN_RETRY_FORBIDDEN",
  "WORKFLOW_INPUT_INVALID",
  "WORKFLOW_OUTPUT_INVALID",
  "SIDE_EFFECT_STATE_UNKNOWN",
  "WORKFLOW_UNAVAILABLE",
]);

type WorkflowDefinitionServerErrorCode = z.infer<
  typeof workflowDefinitionServerErrorCodeSchema
>;

const workflowDefinitionErrorEnvelopeSchema = z
  .object({
    detail: z
      .object({
        code: workflowDefinitionServerErrorCodeSchema,
        message: z.string().min(1).max(2_048),
        request_id: requestIdSchema,
      })
      .strict(),
  })
  .strict();

const SAFE_SERVER_MESSAGES: Record<WorkflowDefinitionServerErrorCode, string> =
  {
    WORKFLOW_NOT_FOUND: "Workflow was not found.",
    WORKFLOW_FORBIDDEN: "Workflow action is forbidden.",
    WORKFLOW_DRAFT_CONFLICT: "Workflow draft conflict.",
    WORKFLOW_DRAFT_INVALID: "Workflow draft is invalid.",
    WORKFLOW_VERSION_NOT_EXECUTABLE: "Workflow version is not executable.",
    WORKFLOW_RUN_CONFLICT: "Workflow Run conflict.",
    WORKFLOW_RUN_NOT_RESUMABLE: "Workflow Run is not resumable.",
    WORKFLOW_RUN_RETRY_FORBIDDEN: "Workflow Run cannot be retried.",
    WORKFLOW_INPUT_INVALID: "Workflow input is invalid.",
    WORKFLOW_OUTPUT_INVALID: "Workflow output is invalid.",
    SIDE_EFFECT_STATE_UNKNOWN: "Workflow side-effect state is unknown.",
    WORKFLOW_UNAVAILABLE: "Workflow is temporarily unavailable.",
  };

const WORKFLOW_SERVER_ERROR_STATUS = {
  WORKFLOW_NOT_FOUND: 404,
  WORKFLOW_FORBIDDEN: 403,
  WORKFLOW_DRAFT_CONFLICT: 409,
  WORKFLOW_DRAFT_INVALID: 422,
  WORKFLOW_VERSION_NOT_EXECUTABLE: 409,
  WORKFLOW_RUN_CONFLICT: 409,
  WORKFLOW_RUN_NOT_RESUMABLE: 409,
  WORKFLOW_RUN_RETRY_FORBIDDEN: 409,
  WORKFLOW_INPUT_INVALID: 422,
  WORKFLOW_OUTPUT_INVALID: 422,
  SIDE_EFFECT_STATE_UNKNOWN: 409,
  WORKFLOW_UNAVAILABLE: 503,
} as const satisfies Record<WorkflowDefinitionServerErrorCode, number>;

export type WorkflowDefinitionReadOptions = Readonly<{
  signal: AbortSignal;
}>;

export type WorkflowDefinitionMutationOptions = Readonly<{
  signal: AbortSignal;
  idempotencyKey: string;
}>;

export type WorkflowDefinitionListInput =
  Partial<WorkflowDefinitionListQueryV1>;

export type WorkflowVersionListInput = Partial<WorkflowVersionListQueryV1>;

function inputInvalid(): ProjectWorkflowApiError {
  return new ProjectWorkflowApiError(
    422,
    "WORKFLOW_INPUT_INVALID",
    SAFE_SERVER_MESSAGES.WORKFLOW_INPUT_INVALID,
  );
}

function parseInput<T>(schema: ZodType<T>, value: unknown): T {
  const parsed = schema.safeParse(value);
  if (!parsed.success) throw inputInvalid();
  return parsed.data;
}

function parseWorkflowId(value: string): string {
  return parseInput(canonicalUuidSchema, value);
}

function parseSlotId(value: string): string {
  return parseInput(slotIdPathSchema, value);
}

function projectWorkflowBaseURL(scope: ProjectClientScope): string {
  const parsed = parseInput(projectClientScopeSchema, scope);
  return `${getBackendBaseURL()}/api/projects/${encodeURIComponent(parsed.projectId)}/workflows`;
}

function workflowDefinitionPath(
  scope: ProjectClientScope,
  workflowId: string,
): string {
  return `${projectWorkflowBaseURL(scope)}/${encodeURIComponent(parseWorkflowId(workflowId))}`;
}

function workflowVersionPath(
  scope: ProjectClientScope,
  workflowId: string,
  versionId: string,
): string {
  return `${workflowDefinitionPath(scope, workflowId)}/versions/${encodeURIComponent(parseWorkflowId(versionId))}`;
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

async function request(input: string, init: RequestInit): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (error instanceof ProjectWorkflowApiError || isAbortError(error)) {
      throw error;
    }
    if (error instanceof AuthRequiredError) {
      throw new ProjectWorkflowApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required.",
      );
    }
    throw new ProjectWorkflowApiError(
      0,
      "WORKFLOW_NETWORK_ERROR",
      "Workflow service is temporarily unavailable.",
    );
  }
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ProjectWorkflowApiError(
      response.status,
      "WORKFLOW_RESPONSE_INVALID",
      response.ok
        ? "Workflow response was invalid."
        : "Workflow request failed.",
    );
  }
}

function throwInvalidResponse(response: Response): never {
  throw new ProjectWorkflowApiError(
    response.status,
    "WORKFLOW_RESPONSE_INVALID",
    response.ok ? "Workflow response was invalid." : "Workflow request failed.",
  );
}

async function readResponse<T>(
  response: Response,
  expectedStatus: number,
  schema: ZodType<T>,
): Promise<T> {
  const payload = await readJson(response);
  if (!response.ok) {
    const parsed = workflowDefinitionErrorEnvelopeSchema.safeParse(payload);
    if (!parsed.success) throwInvalidResponse(response);
    const { code } = parsed.data.detail;
    if (WORKFLOW_SERVER_ERROR_STATUS[code] !== response.status) {
      throwInvalidResponse(response);
    }
    throw new ProjectWorkflowApiError(
      response.status,
      code,
      SAFE_SERVER_MESSAGES[code],
    );
  }
  if (response.status !== expectedStatus) throwInvalidResponse(response);
  const parsed = schema.safeParse(payload);
  if (!parsed.success) throwInvalidResponse(response);
  return parsed.data;
}

function jsonHeaders(idempotencyKey?: string): Headers {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (idempotencyKey !== undefined) {
    headers.set(
      "Idempotency-Key",
      parseInput(idempotencyKeySchema, idempotencyKey),
    );
  }
  return headers;
}

function definitionListSearchParams(
  input: WorkflowDefinitionListInput = {},
): string {
  const result = workflowDefinitionListQueryV1Schema.safeParse(input);
  if (!result.success) throw inputInvalid();
  const parsed: WorkflowDefinitionListQueryV1 = result.data;
  const params = new URLSearchParams();
  if (parsed.query !== null) params.set("query", parsed.query);
  params.set("lifecycle", parsed.lifecycle);
  params.set("publication", parsed.publication);
  params.set("sort", parsed.sort);
  if (parsed.cursor !== null) params.set("cursor", parsed.cursor);
  params.set("limit", String(parsed.limit));
  return params.toString();
}

function versionListSearchParams(input: WorkflowVersionListInput = {}): string {
  const result = workflowVersionListQueryV1Schema.safeParse(input);
  if (!result.success) throw inputInvalid();
  const parsed: WorkflowVersionListQueryV1 = result.data;
  const params = new URLSearchParams();
  if (parsed.cursor !== null) params.set("cursor", parsed.cursor);
  params.set("limit", String(parsed.limit));
  return params.toString();
}

export function createWorkflowDefinitionIdempotencyKey(): string {
  return parseInput(idempotencyKeySchema, globalThis.crypto.randomUUID());
}

export async function listWorkflowDefinitions(
  scope: ProjectClientScope,
  query: WorkflowDefinitionListInput = {},
  options: WorkflowDefinitionReadOptions,
): Promise<WorkflowDefinitionPageV1> {
  const response = await request(
    `${projectWorkflowBaseURL(scope)}?${definitionListSearchParams(query)}`,
    { signal: options.signal },
  );
  return readResponse(response, 200, workflowDefinitionPageV1Schema);
}

export async function createWorkflowDefinition(
  scope: ProjectClientScope,
  body: WorkflowDefinitionCreateRequestV1,
  options: WorkflowDefinitionMutationOptions,
): Promise<WorkflowDefinitionResponseV1> {
  const parsedBody = parseInput(workflowDefinitionCreateRequestV1Schema, body);
  const response = await request(projectWorkflowBaseURL(scope), {
    method: "POST",
    headers: jsonHeaders(options.idempotencyKey),
    body: JSON.stringify(parsedBody),
    signal: options.signal,
  });
  return readResponse(response, 201, workflowDefinitionResponseV1Schema);
}

export async function readWorkflowDefinition(
  scope: ProjectClientScope,
  workflowId: string,
  options: WorkflowDefinitionReadOptions,
): Promise<WorkflowDefinitionResponseV1> {
  const response = await request(workflowDefinitionPath(scope, workflowId), {
    signal: options.signal,
  });
  return readResponse(response, 200, workflowDefinitionResponseV1Schema);
}

export async function archiveWorkflowDefinition(
  scope: ProjectClientScope,
  workflowId: string,
  body: WorkflowDefinitionArchiveRequestV1,
  options: WorkflowDefinitionMutationOptions,
): Promise<WorkflowDefinitionResponseV1> {
  const parsedBody = parseInput(workflowDefinitionArchiveRequestV1Schema, body);
  const response = await request(
    `${workflowDefinitionPath(scope, workflowId)}/archive`,
    {
      method: "POST",
      headers: jsonHeaders(options.idempotencyKey),
      body: JSON.stringify(parsedBody),
      signal: options.signal,
    },
  );
  return readResponse(response, 200, workflowDefinitionResponseV1Schema);
}

export async function readWorkflowDraft(
  scope: ProjectClientScope,
  workflowId: string,
  options: WorkflowDefinitionReadOptions,
): Promise<WorkflowDraftResponseV1> {
  const response = await request(
    `${workflowDefinitionPath(scope, workflowId)}/draft`,
    { signal: options.signal },
  );
  return readResponse(response, 200, workflowDraftResponseV1Schema);
}

export async function saveWorkflowDraft(
  scope: ProjectClientScope,
  workflowId: string,
  body: WorkflowDraftSaveRequestV1,
  options: WorkflowDefinitionMutationOptions,
): Promise<WorkflowDraftResponseV1> {
  const parsedBody = parseInput(workflowDraftSaveRequestV1Schema, body);
  const response = await request(
    `${workflowDefinitionPath(scope, workflowId)}/draft`,
    {
      method: "PUT",
      headers: jsonHeaders(options.idempotencyKey),
      body: JSON.stringify(parsedBody),
      signal: options.signal,
    },
  );
  return readResponse(response, 200, workflowDraftResponseV1Schema);
}

export async function validateWorkflowDraft(
  scope: ProjectClientScope,
  workflowId: string,
  body: WorkflowDraftValidateRequestV1,
  options: WorkflowDefinitionReadOptions,
): Promise<WorkflowDraftValidationResponseV1> {
  const parsedBody = parseInput(workflowDraftValidateRequestV1Schema, body);
  const response = await request(
    `${workflowDefinitionPath(scope, workflowId)}/validate`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify(parsedBody),
      signal: options.signal,
    },
  );
  return readResponse(response, 200, workflowDraftValidationResponseV1Schema);
}

export async function publishWorkflowDraft(
  scope: ProjectClientScope,
  workflowId: string,
  body: WorkflowPublishRequestV1,
  options: WorkflowDefinitionMutationOptions,
): Promise<WorkflowPublishResponseV1> {
  const parsedBody = parseInput(workflowPublishRequestV1Schema, body);
  const response = await request(
    `${workflowDefinitionPath(scope, workflowId)}/publish`,
    {
      method: "POST",
      headers: jsonHeaders(options.idempotencyKey),
      body: JSON.stringify(parsedBody),
      signal: options.signal,
    },
  );
  return readResponse(response, 201, workflowPublishResponseV1Schema);
}

export async function listWorkflowVersions(
  scope: ProjectClientScope,
  workflowId: string,
  query: WorkflowVersionListInput = {},
  options: WorkflowDefinitionReadOptions,
): Promise<WorkflowVersionPageV1> {
  const response = await request(
    `${workflowDefinitionPath(scope, workflowId)}/versions?${versionListSearchParams(query)}`,
    { signal: options.signal },
  );
  return readResponse(response, 200, workflowVersionPageV1Schema);
}

export async function readWorkflowVersion(
  scope: ProjectClientScope,
  workflowId: string,
  versionId: string,
  options: WorkflowDefinitionReadOptions,
): Promise<WorkflowVersionResponseV1> {
  const response = await request(
    workflowVersionPath(scope, workflowId, versionId),
    { signal: options.signal },
  );
  return readResponse(response, 200, workflowVersionResponseV1Schema);
}

export async function putWorkflowDraftGrantIntent(
  scope: ProjectClientScope,
  workflowId: string,
  slotId: string,
  body: WorkflowCredentialGrantMutationRequestV1,
  options: WorkflowDefinitionMutationOptions,
): Promise<WorkflowDraftGrantIntentResponseV1> {
  const parsedBody = parseInput(
    workflowCredentialGrantMutationRequestV1Schema,
    body,
  );
  const response = await request(
    `${workflowDefinitionPath(scope, workflowId)}/draft/credential-grant-intents/${encodeURIComponent(parseSlotId(slotId))}`,
    {
      method: "PUT",
      headers: jsonHeaders(options.idempotencyKey),
      body: JSON.stringify(parsedBody),
      signal: options.signal,
    },
  );
  return readResponse(response, 200, workflowDraftGrantIntentResponseV1Schema);
}

export async function deleteWorkflowDraftGrantIntent(
  scope: ProjectClientScope,
  workflowId: string,
  slotId: string,
  options: WorkflowDefinitionMutationOptions,
): Promise<WorkflowDraftGrantIntentDeleteResponseV1> {
  const response = await request(
    `${workflowDefinitionPath(scope, workflowId)}/draft/credential-grant-intents/${encodeURIComponent(parseSlotId(slotId))}`,
    {
      method: "DELETE",
      headers: jsonHeaders(options.idempotencyKey),
      signal: options.signal,
    },
  );
  return readResponse(
    response,
    200,
    workflowDraftGrantIntentDeleteResponseV1Schema,
  );
}

export async function putWorkflowVersionGrant(
  scope: ProjectClientScope,
  workflowId: string,
  versionId: string,
  slotId: string,
  body: WorkflowCredentialGrantMutationRequestV1,
  options: WorkflowDefinitionMutationOptions,
): Promise<WorkflowCredentialGrantResponseV1> {
  const parsedBody = parseInput(
    workflowCredentialGrantMutationRequestV1Schema,
    body,
  );
  const response = await request(
    `${workflowVersionPath(scope, workflowId, versionId)}/credential-grants/${encodeURIComponent(parseSlotId(slotId))}`,
    {
      method: "PUT",
      headers: jsonHeaders(options.idempotencyKey),
      body: JSON.stringify(parsedBody),
      signal: options.signal,
    },
  );
  return readResponse(response, 200, workflowCredentialGrantResponseV1Schema);
}

export async function revokeWorkflowVersionGrant(
  scope: ProjectClientScope,
  workflowId: string,
  versionId: string,
  slotId: string,
  options: WorkflowDefinitionMutationOptions,
): Promise<WorkflowCredentialGrantResponseV1> {
  const response = await request(
    `${workflowVersionPath(scope, workflowId, versionId)}/credential-grants/${encodeURIComponent(parseSlotId(slotId))}`,
    {
      method: "DELETE",
      headers: jsonHeaders(options.idempotencyKey),
      signal: options.signal,
    },
  );
  return readResponse(response, 200, workflowCredentialGrantResponseV1Schema);
}

export type WorkflowDefinitionTransport = {
  listDefinitions(
    scope: ProjectClientScope,
    query: WorkflowDefinitionListInput,
    options: WorkflowDefinitionReadOptions,
  ): Promise<WorkflowDefinitionPageV1>;
  createDefinition(
    scope: ProjectClientScope,
    body: WorkflowDefinitionCreateRequestV1,
    options: WorkflowDefinitionMutationOptions,
  ): Promise<WorkflowDefinitionResponseV1>;
  readDefinition(
    scope: ProjectClientScope,
    workflowId: string,
    options: WorkflowDefinitionReadOptions,
  ): Promise<WorkflowDefinitionResponseV1>;
  archiveDefinition(
    scope: ProjectClientScope,
    workflowId: string,
    body: WorkflowDefinitionArchiveRequestV1,
    options: WorkflowDefinitionMutationOptions,
  ): Promise<WorkflowDefinitionResponseV1>;
  readDraft(
    scope: ProjectClientScope,
    workflowId: string,
    options: WorkflowDefinitionReadOptions,
  ): Promise<WorkflowDraftResponseV1>;
  saveDraft(
    scope: ProjectClientScope,
    workflowId: string,
    body: WorkflowDraftSaveRequestV1,
    options: WorkflowDefinitionMutationOptions,
  ): Promise<WorkflowDraftResponseV1>;
  validateDraft(
    scope: ProjectClientScope,
    workflowId: string,
    body: WorkflowDraftValidateRequestV1,
    options: WorkflowDefinitionReadOptions,
  ): Promise<WorkflowDraftValidationResponseV1>;
  publishDraft(
    scope: ProjectClientScope,
    workflowId: string,
    body: WorkflowPublishRequestV1,
    options: WorkflowDefinitionMutationOptions,
  ): Promise<WorkflowPublishResponseV1>;
  listVersions(
    scope: ProjectClientScope,
    workflowId: string,
    query: WorkflowVersionListInput,
    options: WorkflowDefinitionReadOptions,
  ): Promise<WorkflowVersionPageV1>;
  readVersion(
    scope: ProjectClientScope,
    workflowId: string,
    versionId: string,
    options: WorkflowDefinitionReadOptions,
  ): Promise<WorkflowVersionResponseV1>;
  putDraftGrantIntent(
    scope: ProjectClientScope,
    workflowId: string,
    slotId: string,
    body: WorkflowCredentialGrantMutationRequestV1,
    options: WorkflowDefinitionMutationOptions,
  ): Promise<WorkflowDraftGrantIntentResponseV1>;
  deleteDraftGrantIntent(
    scope: ProjectClientScope,
    workflowId: string,
    slotId: string,
    options: WorkflowDefinitionMutationOptions,
  ): Promise<WorkflowDraftGrantIntentDeleteResponseV1>;
  putVersionGrant(
    scope: ProjectClientScope,
    workflowId: string,
    versionId: string,
    slotId: string,
    body: WorkflowCredentialGrantMutationRequestV1,
    options: WorkflowDefinitionMutationOptions,
  ): Promise<WorkflowCredentialGrantResponseV1>;
  revokeVersionGrant(
    scope: ProjectClientScope,
    workflowId: string,
    versionId: string,
    slotId: string,
    options: WorkflowDefinitionMutationOptions,
  ): Promise<WorkflowCredentialGrantResponseV1>;
};

export function createWorkflowDefinitionTransport(): WorkflowDefinitionTransport {
  return {
    listDefinitions: listWorkflowDefinitions,
    createDefinition: createWorkflowDefinition,
    readDefinition: readWorkflowDefinition,
    archiveDefinition: archiveWorkflowDefinition,
    readDraft: readWorkflowDraft,
    saveDraft: saveWorkflowDraft,
    validateDraft: validateWorkflowDraft,
    publishDraft: publishWorkflowDraft,
    listVersions: listWorkflowVersions,
    readVersion: readWorkflowVersion,
    putDraftGrantIntent: putWorkflowDraftGrantIntent,
    deleteDraftGrantIntent: deleteWorkflowDraftGrantIntent,
    putVersionGrant: putWorkflowVersionGrant,
    revokeVersionGrant: revokeWorkflowVersionGrant,
  };
}
