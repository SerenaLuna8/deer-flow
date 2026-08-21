import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  cancelSkillBuilderSessionInputSchema,
  commitSkillBuilderSessionInputSchema,
  createSkillBuilderRevisionInputSchema,
  createSkillBuilderSessionInputSchema,
  skillBuilderCommitResponseSchema,
  skillBuilderActivityListResponseSchema,
  skillBuilderActivitySchema,
  skillBuilderSessionListResponseSchema,
  skillBuilderSessionResponseSchema,
  skillBuilderTurnInputSchema,
  skillBuilderTurnResponseSchema,
  setSkillBuilderExecutionPreferenceInputSchema,
  validateSkillBuilderSessionInputSchema,
  type CancelSkillBuilderSessionInput,
  type CommitSkillBuilderSessionInput,
  type CreateSkillBuilderRevisionInput,
  type CreateSkillBuilderSessionInput,
  type SkillBuilderTurnInput,
  type SkillBuilderActivity,
  type SetSkillBuilderExecutionPreferenceInput,
  type ValidateSkillBuilderSessionInput,
} from "./types";

const uuidSchema = z.string().uuid();

const serverErrorSchema = z
  .object({
    detail: z.union([
      z.string().trim().min(1),
      z
        .object({
          code: z.string().trim().min(1),
          message: z.string().trim().min(1),
          request_id: z.string().trim().min(1).optional(),
        })
        .strict(),
    ]),
  })
  .strict();

export type SkillBuilderApiErrorCode =
  | "AUTH_REQUIRED"
  | "SKILL_BUILDER_CONFLICT"
  | "SKILL_BUILDER_FORBIDDEN"
  | "SKILL_BUILDER_NOT_FOUND"
  | "SKILL_BUILDER_LIMIT_EXCEEDED"
  | "SKILL_BUILDER_VALIDATION_FAILED"
  | "SKILL_BUILDER_UNAVAILABLE"
  | "SKILL_BUILDER_NETWORK_ERROR"
  | "SKILL_BUILDER_RESPONSE_INVALID";

export class SkillBuilderApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: SkillBuilderApiErrorCode,
    message: string,
    /** Stable Gateway error code (e.g. SKILL_BUILDER_MODEL_UNAVAILABLE). */
    readonly serverCode: string | null = null,
  ) {
    super(message);
    this.name = "SkillBuilderApiError";
  }
}

function baseURL(projectId: string) {
  return `${getBackendBaseURL()}/api/projects/${uuidSchema.parse(projectId)}/skill-builder/sessions`;
}

function sessionURL(projectId: string, sessionId: string) {
  return `${baseURL(projectId)}/${uuidSchema.parse(sessionId)}`;
}

export function skillBuilderActivityStreamURL(
  projectId: string,
  sessionId: string,
) {
  const query = new URLSearchParams({ after_seq: "0" });
  return `${sessionURL(projectId, sessionId)}/activities/stream?${query.toString()}`;
}

export function parseSkillBuilderActivity(
  value: unknown,
): SkillBuilderActivity {
  return skillBuilderActivitySchema.parse(value);
}

function safeCode(status: number): SkillBuilderApiErrorCode {
  if (status === 401) return "AUTH_REQUIRED";
  if (status === 403) return "SKILL_BUILDER_FORBIDDEN";
  if (status === 404) return "SKILL_BUILDER_NOT_FOUND";
  if (status === 409) return "SKILL_BUILDER_CONFLICT";
  if (status === 429) return "SKILL_BUILDER_LIMIT_EXCEEDED";
  if (status === 422) return "SKILL_BUILDER_VALIDATION_FAILED";
  return "SKILL_BUILDER_UNAVAILABLE";
}

async function request<TSchema extends z.ZodType>(
  url: string,
  schema: TSchema,
  init?: RequestInit,
): Promise<z.output<TSchema>> {
  let response: Response;
  try {
    response = await fetchWithAuth(url, init);
  } catch (error) {
    if (error instanceof AuthRequiredError) {
      throw new SkillBuilderApiError(401, "AUTH_REQUIRED", "需要重新登录");
    }
    if (
      typeof error === "object" &&
      error !== null &&
      "name" in error &&
      error.name === "AbortError"
    ) {
      throw error;
    }
    throw new SkillBuilderApiError(
      0,
      "SKILL_BUILDER_NETWORK_ERROR",
      "无法连接 Skill 设计服务",
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    if (!response.ok) {
      throw new SkillBuilderApiError(
        response.status,
        safeCode(response.status),
        "Skill 设计请求失败",
      );
    }
    throw new SkillBuilderApiError(
      response.status,
      "SKILL_BUILDER_RESPONSE_INVALID",
      "Skill 设计服务返回了无效响应",
    );
  }

  if (!response.ok) {
    const parsedError = serverErrorSchema.safeParse(payload);
    const detail = parsedError.success ? parsedError.data.detail : null;
    const message =
      typeof detail === "string"
        ? detail
        : (detail?.message ?? "Skill 设计请求失败");
    throw new SkillBuilderApiError(
      response.status,
      safeCode(response.status),
      message,
      typeof detail === "object" && detail !== null ? detail.code : null,
    );
  }

  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new SkillBuilderApiError(
      response.status,
      "SKILL_BUILDER_RESPONSE_INVALID",
      "Skill 设计服务返回了无效响应",
    );
  }
  return parsed.data;
}

export function createSkillBuilderSession(
  projectId: string,
  input: CreateSkillBuilderSessionInput,
  signal?: AbortSignal,
) {
  const body = createSkillBuilderSessionInputSchema.parse(input);
  return request(baseURL(projectId), skillBuilderSessionResponseSchema, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export function createSkillBuilderRevisionSession(
  projectId: string,
  input: CreateSkillBuilderRevisionInput,
  signal?: AbortSignal,
) {
  const body = createSkillBuilderRevisionInputSchema.parse(input);
  return request(baseURL(projectId), skillBuilderSessionResponseSchema, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export function listSkillBuilderSessions(
  projectId: string,
  signal?: AbortSignal,
) {
  return request(baseURL(projectId), skillBuilderSessionListResponseSchema, {
    signal,
  });
}

export function getSkillBuilderSession(
  projectId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  return request(
    sessionURL(projectId, sessionId),
    skillBuilderSessionResponseSchema,
    { signal },
  );
}

export function getSkillBuilderSessionByVersion(
  projectId: string,
  versionId: string,
  signal?: AbortSignal,
) {
  return request(
    `${baseURL(projectId)}/by-version/${uuidSchema.parse(versionId)}`,
    skillBuilderSessionResponseSchema,
    { signal },
  );
}

export function listSkillBuilderActivities(
  projectId: string,
  sessionId: string,
  afterSeq = "0",
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ after_seq: afterSeq });
  return request(
    `${sessionURL(projectId, sessionId)}/activities?${query.toString()}`,
    skillBuilderActivityListResponseSchema,
    { signal },
  );
}

export function submitSkillBuilderTurn(
  projectId: string,
  sessionId: string,
  input: SkillBuilderTurnInput,
  signal?: AbortSignal,
) {
  const body = skillBuilderTurnInputSchema.parse(input);
  return request(
    `${sessionURL(projectId, sessionId)}/turns`,
    skillBuilderTurnResponseSchema,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}

export function setSkillBuilderExecutionPreference(
  projectId: string,
  sessionId: string,
  input: SetSkillBuilderExecutionPreferenceInput,
  signal?: AbortSignal,
) {
  const body = setSkillBuilderExecutionPreferenceInputSchema.parse(input);
  return request(
    `${sessionURL(projectId, sessionId)}/execution-preference`,
    skillBuilderSessionResponseSchema,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}

export function stopSkillBuilderTurn(
  projectId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  return request(
    `${sessionURL(projectId, sessionId)}/turns/stop`,
    skillBuilderSessionResponseSchema,
    { method: "POST", signal },
  );
}

export function validateSkillBuilderSession(
  projectId: string,
  sessionId: string,
  input: ValidateSkillBuilderSessionInput,
  signal?: AbortSignal,
) {
  const body = validateSkillBuilderSessionInputSchema.parse(input);
  return request(
    `${sessionURL(projectId, sessionId)}/validate`,
    skillBuilderSessionResponseSchema,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}

export function commitSkillBuilderSession(
  projectId: string,
  sessionId: string,
  input: CommitSkillBuilderSessionInput,
  signal?: AbortSignal,
) {
  const body = commitSkillBuilderSessionInputSchema.parse(input);
  return request(
    `${sessionURL(projectId, sessionId)}/commit`,
    skillBuilderCommitResponseSchema,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}

export function cancelSkillBuilderSession(
  projectId: string,
  sessionId: string,
  input: CancelSkillBuilderSessionInput,
  signal?: AbortSignal,
) {
  const body = cancelSkillBuilderSessionInputSchema.parse(input);
  return request(
    `${sessionURL(projectId, sessionId)}/cancel`,
    skillBuilderSessionResponseSchema,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}
