import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";
import {
  projectClientScopeSchema,
  type ProjectClientScope,
} from "@/core/private-work/types";

import {
  automationDeleteSchema,
  automationIdempotencyKeySchema,
  automationIdSchema,
  automationListFiltersSchema,
  automationListSchema,
  automationRunListSchema,
  automationRunSchema,
  automationSchema,
  automationVersionInputSchema,
  createAutomationInputSchema,
  updateAutomationInputSchema,
  type Automation,
  type AutomationDelete,
  type AutomationListFilters,
  type AutomationRun,
  type CreateAutomationInput,
  type UpdateAutomationInput,
} from "./types";

const serverErrorCodeSchema = z.enum([
  "AUTOMATION_NOT_FOUND",
  "AUTOMATION_FORBIDDEN",
  "AUTOMATION_CONFLICT",
  "AUTOMATION_INVALID",
  "AUTOMATION_VERSION_CONFLICT",
  "AUTOMATION_ACTIVE_RUN",
  "AUTOMATION_ONCE_EXPIRED",
  "AUTOMATION_CONCURRENCY_LIMIT",
  "AUTOMATION_UNAVAILABLE",
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

const SAFE_SERVER_MESSAGES: Record<
  z.infer<typeof serverErrorCodeSchema>,
  string
> = {
  AUTOMATION_NOT_FOUND: "Automation was not found.",
  AUTOMATION_FORBIDDEN: "Automation action is forbidden.",
  AUTOMATION_CONFLICT: "Automation resource conflict.",
  AUTOMATION_INVALID: "Automation request is invalid.",
  AUTOMATION_VERSION_CONFLICT: "Automation version conflict.",
  AUTOMATION_ACTIVE_RUN: "Automation has an active run.",
  AUTOMATION_ONCE_EXPIRED: "Automation one-time schedule has expired.",
  AUTOMATION_CONCURRENCY_LIMIT: "Automation concurrency limit was reached.",
  AUTOMATION_UNAVAILABLE: "Automation is temporarily unavailable.",
};

export const AUTOMATION_ERROR_CODES = [
  ...serverErrorCodeSchema.options,
  "AUTH_REQUIRED",
  "AUTOMATION_NETWORK_ERROR",
  "AUTOMATION_RESPONSE_INVALID",
  "AUTOMATION_ERROR_RESPONSE_INVALID",
  "AUTOMATION_VALIDATION_FAILED",
] as const;

export type AutomationErrorCode = (typeof AUTOMATION_ERROR_CODES)[number];

export class AutomationApiError extends Error {
  readonly status: number;
  readonly code: AutomationErrorCode;

  constructor(status: number, code: AutomationErrorCode, message: string) {
    super(message);
    this.name = "AutomationApiError";
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

function validationError(): AutomationApiError {
  return new AutomationApiError(
    422,
    "AUTOMATION_VALIDATION_FAILED",
    "Automation validation failed",
  );
}

export function parseAutomationInput<T>(
  schema: z.ZodType<T>,
  value: unknown,
): T {
  const parsed = schema.safeParse(value);
  if (!parsed.success) throw validationError();
  return parsed.data;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new AutomationApiError(
      response.status,
      response.ok
        ? "AUTOMATION_RESPONSE_INVALID"
        : "AUTOMATION_ERROR_RESPONSE_INVALID",
      response.ok
        ? "Automation response was invalid"
        : "Automation request failed",
    );
  }
}

export async function requestAutomation(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (error instanceof AutomationApiError || isAbortError(error)) throw error;
    if (error instanceof AuthRequiredError) {
      throw new AutomationApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new AutomationApiError(
      0,
      "AUTOMATION_NETWORK_ERROR",
      "Automation service is unavailable",
    );
  }
}

async function throwResponseError(response: Response): Promise<never> {
  const parsed = errorEnvelopeSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new AutomationApiError(
      response.status,
      "AUTOMATION_ERROR_RESPONSE_INVALID",
      "Automation request failed",
    );
  }
  const code = parsed.data.detail.code;
  throw new AutomationApiError(
    response.status,
    code,
    SAFE_SERVER_MESSAGES[code],
  );
}

export async function readAutomationResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
): Promise<T> {
  if (!response.ok) await throwResponseError(response);
  const parsed = schema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new AutomationApiError(
      response.status,
      "AUTOMATION_RESPONSE_INVALID",
      "Automation response was invalid",
    );
  }
  return parsed.data;
}

export function automationBaseURL(scope: ProjectClientScope): string {
  const parsed = parseAutomationInput(projectClientScopeSchema, scope);
  return `${getBackendBaseURL()}/api/projects/${encodeURIComponent(
    parsed.projectId,
  )}/automations`;
}

function automationPath(scope: ProjectClientScope, taskId: string): string {
  const parsedTaskId = parseAutomationInput(automationIdSchema, taskId);
  return `${automationBaseURL(scope)}/${encodeURIComponent(parsedTaskId)}`;
}

function listQuery(filters: AutomationListFilters = {}): string {
  const parsed = parseAutomationInput(automationListFiltersSchema, filters);
  const params = new URLSearchParams({
    limit: String(parsed.limit),
    offset: String(parsed.offset),
  });
  return params.toString();
}

export async function listAutomations(
  scope: ProjectClientScope,
  filters: AutomationListFilters = {},
  signal?: AbortSignal,
): Promise<Automation[]> {
  const response = await requestAutomation(
    `${automationBaseURL(scope)}?${listQuery(filters)}`,
    { signal },
  );
  return (await readAutomationResponse(response, automationListSchema)).items;
}

export async function listThreadAutomations(
  scope: ProjectClientScope,
  threadId: string,
  filters: AutomationListFilters = {},
  signal?: AbortSignal,
): Promise<Automation[]> {
  const parsedThreadId = parseAutomationInput(z.string().uuid(), threadId);
  const response = await requestAutomation(
    `${automationBaseURL(scope)}/threads/${encodeURIComponent(
      parsedThreadId,
    )}?${listQuery(filters)}`,
    { signal },
  );
  return (await readAutomationResponse(response, automationListSchema)).items;
}

export async function getAutomation(
  scope: ProjectClientScope,
  taskId: string,
  signal?: AbortSignal,
): Promise<Automation> {
  const response = await requestAutomation(automationPath(scope, taskId), {
    signal,
  });
  return readAutomationResponse(response, automationSchema);
}

export async function createAutomation(
  scope: ProjectClientScope,
  input: CreateAutomationInput,
  signal?: AbortSignal,
): Promise<Automation> {
  const body = parseAutomationInput(createAutomationInputSchema, input);
  const response = await requestAutomation(automationBaseURL(scope), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return readAutomationResponse(response, automationSchema);
}

export async function updateAutomation(
  scope: ProjectClientScope,
  taskId: string,
  input: UpdateAutomationInput,
  signal?: AbortSignal,
): Promise<Automation> {
  const body = parseAutomationInput(updateAutomationInputSchema, input);
  const response = await requestAutomation(automationPath(scope, taskId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return readAutomationResponse(response, automationSchema);
}

async function versionMutation(
  scope: ProjectClientScope,
  taskId: string,
  action: "pause" | "resume" | "delete",
  expectedVersion: number,
  signal?: AbortSignal,
) {
  const body = parseAutomationInput(automationVersionInputSchema, {
    expected_version: expectedVersion,
  });
  const path = automationPath(scope, taskId);
  const response = await requestAutomation(
    action === "delete" ? path : `${path}/${action}`,
    {
      method: action === "delete" ? "DELETE" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return { action, response } as const;
}

export async function pauseAutomation(
  scope: ProjectClientScope,
  taskId: string,
  expectedVersion: number,
  signal?: AbortSignal,
): Promise<Automation> {
  const { response } = await versionMutation(
    scope,
    taskId,
    "pause",
    expectedVersion,
    signal,
  );
  return readAutomationResponse(response, automationSchema);
}

export async function resumeAutomation(
  scope: ProjectClientScope,
  taskId: string,
  expectedVersion: number,
  signal?: AbortSignal,
): Promise<Automation> {
  const { response } = await versionMutation(
    scope,
    taskId,
    "resume",
    expectedVersion,
    signal,
  );
  return readAutomationResponse(response, automationSchema);
}

export async function deleteAutomation(
  scope: ProjectClientScope,
  taskId: string,
  expectedVersion: number,
  signal?: AbortSignal,
): Promise<AutomationDelete> {
  const { response } = await versionMutation(
    scope,
    taskId,
    "delete",
    expectedVersion,
    signal,
  );
  return readAutomationResponse(response, automationDeleteSchema);
}

export function createAutomationIdempotencyKey(
  randomUUID: () => string = () => globalThis.crypto.randomUUID(),
): string {
  return parseAutomationInput(automationIdempotencyKeySchema, randomUUID());
}

export async function triggerAutomation(
  scope: ProjectClientScope,
  taskId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<AutomationRun> {
  const parsedKey = parseAutomationInput(
    automationIdempotencyKeySchema,
    idempotencyKey,
  );
  const response = await requestAutomation(
    `${automationPath(scope, taskId)}/trigger`,
    {
      method: "POST",
      headers: { "Idempotency-Key": parsedKey },
      signal,
    },
  );
  return readAutomationResponse(response, automationRunSchema);
}

export async function listAutomationRuns(
  scope: ProjectClientScope,
  taskId: string,
  filters: AutomationListFilters = {},
  signal?: AbortSignal,
): Promise<AutomationRun[]> {
  const response = await requestAutomation(
    `${automationPath(scope, taskId)}/runs?${listQuery(filters)}`,
    { signal },
  );
  return (await readAutomationResponse(response, automationRunListSchema))
    .items;
}
