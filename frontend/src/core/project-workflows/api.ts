import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";
import {
  projectClientScopeSchema,
  type ProjectClientScope,
} from "@/core/private-work/types";

import {
  nodeCatalogResponseV1Schema,
  type NodeCatalogResponseV1,
} from "./catalog";
import {
  workflowProjectReadinessV1Schema,
  type WorkflowProjectReadinessV1,
} from "./transport";

const workflowServerErrorCodeSchema = z.enum([
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

type WorkflowServerErrorCode = z.infer<typeof workflowServerErrorCodeSchema>;

const workflowErrorEnvelopeSchema = z
  .object({
    detail: z
      .object({
        code: workflowServerErrorCodeSchema,
        message: z.string().min(1).max(2_048),
        request_id: z
          .string()
          .min(1)
          .max(512)
          .regex(/^[\x20-\x7e]+$/),
      })
      .strict(),
  })
  .strict();

const SAFE_SERVER_MESSAGES: Record<WorkflowServerErrorCode, string> = {
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
} as const satisfies Record<WorkflowServerErrorCode, number>;

export type ProjectWorkflowApiErrorCode =
  | WorkflowServerErrorCode
  | "AUTH_REQUIRED"
  | "WORKFLOW_NETWORK_ERROR"
  | "WORKFLOW_RESPONSE_INVALID";

export class ProjectWorkflowApiError extends Error {
  readonly status: number;
  readonly code: ProjectWorkflowApiErrorCode;

  constructor(
    status: number,
    code: ProjectWorkflowApiErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "ProjectWorkflowApiError";
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

function projectWorkflowBaseURL(scope: ProjectClientScope): string {
  const parsed = projectClientScopeSchema.safeParse(scope);
  if (!parsed.success) {
    throw new ProjectWorkflowApiError(
      422,
      "WORKFLOW_INPUT_INVALID",
      SAFE_SERVER_MESSAGES.WORKFLOW_INPUT_INVALID,
    );
  }
  return `${getBackendBaseURL()}/api/projects/${encodeURIComponent(parsed.data.projectId)}/workflows`;
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

async function parseReadinessResponse(
  response: Response,
): Promise<WorkflowProjectReadinessV1> {
  const payload = await readJson(response);
  if (!response.ok) {
    const parsedError = workflowErrorEnvelopeSchema.safeParse(payload);
    if (!parsedError.success) {
      throw new ProjectWorkflowApiError(
        response.status,
        "WORKFLOW_RESPONSE_INVALID",
        "Workflow request failed.",
      );
    }
    const { code } = parsedError.data.detail;
    if (WORKFLOW_SERVER_ERROR_STATUS[code] !== response.status) {
      throw new ProjectWorkflowApiError(
        response.status,
        "WORKFLOW_RESPONSE_INVALID",
        "Workflow request failed.",
      );
    }
    throw new ProjectWorkflowApiError(
      response.status,
      code,
      SAFE_SERVER_MESSAGES[code],
    );
  }

  const parsed = workflowProjectReadinessV1Schema.safeParse(payload);
  if (!parsed.success) {
    throw new ProjectWorkflowApiError(
      response.status,
      "WORKFLOW_RESPONSE_INVALID",
      "Workflow response was invalid.",
    );
  }
  return parsed.data;
}

async function parseNodeCatalogResponse(
  response: Response,
): Promise<NodeCatalogResponseV1> {
  const payload = await readJson(response);
  if (!response.ok) {
    const parsedError = workflowErrorEnvelopeSchema.safeParse(payload);
    if (!parsedError.success) {
      throw new ProjectWorkflowApiError(
        response.status,
        "WORKFLOW_RESPONSE_INVALID",
        "Workflow request failed.",
      );
    }
    const { code } = parsedError.data.detail;
    if (WORKFLOW_SERVER_ERROR_STATUS[code] !== response.status) {
      throw new ProjectWorkflowApiError(
        response.status,
        "WORKFLOW_RESPONSE_INVALID",
        "Workflow request failed.",
      );
    }
    throw new ProjectWorkflowApiError(
      response.status,
      code,
      SAFE_SERVER_MESSAGES[code],
    );
  }

  const parsed = nodeCatalogResponseV1Schema.safeParse(payload);
  if (!parsed.success) {
    throw new ProjectWorkflowApiError(
      response.status,
      "WORKFLOW_RESPONSE_INVALID",
      "Workflow response was invalid.",
    );
  }
  return parsed.data;
}

export async function readProjectWorkflowReadiness(
  scope: ProjectClientScope,
  options: { signal: AbortSignal },
): Promise<WorkflowProjectReadinessV1> {
  const response = await request(`${projectWorkflowBaseURL(scope)}/readiness`, {
    signal: options.signal,
  });
  return parseReadinessResponse(response);
}

export async function readProjectWorkflowNodeCatalog(
  scope: ProjectClientScope,
  options: { signal: AbortSignal },
): Promise<NodeCatalogResponseV1> {
  const response = await request(
    `${projectWorkflowBaseURL(scope)}/node-catalog`,
    { signal: options.signal },
  );
  return parseNodeCatalogResponse(response);
}
