import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  agentBuilderCommitResponseSchema,
  agentBuilderSessionListResponseSchema,
  agentBuilderSessionListInputSchema,
  agentBuilderSessionResponseSchema,
  agentBuilderTurnInputSchema,
  cancelAgentBuilderSessionInputSchema,
  commitAgentBuilderSessionInputSchema,
  createAgentBuilderSessionInputSchema,
  type AgentBuilderTurnInput,
  type CancelAgentBuilderSessionInput,
  type CommitAgentBuilderSessionInput,
  type CreateAgentBuilderSessionInput,
  type AgentBuilderSessionListInput,
  type AgentBuilderSessionSummary,
} from "./types";

const uuidSchema = z.string().uuid();
const MAX_AGENT_BUILDER_SESSION_PAGES = 100;
const AGENT_BUILDER_CONTRACT_VERSION = "2";

function sortAgentBuilderSessionsForResume(
  sessions: AgentBuilderSessionSummary[],
): AgentBuilderSessionSummary[] {
  return sessions.sort((left, right) => {
    const updatedOrder =
      Date.parse(right.updated_at) - Date.parse(left.updated_at);
    return updatedOrder || right.id.localeCompare(left.id);
  });
}

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

export type AgentBuilderApiErrorCode =
  | "AUTH_REQUIRED"
  | "AGENT_BUILDER_CONFLICT"
  | "AGENT_BUILDER_FORBIDDEN"
  | "AGENT_BUILDER_NOT_FOUND"
  | "AGENT_BUILDER_VALIDATION_FAILED"
  | "AGENT_BUILDER_UNAVAILABLE"
  | "AGENT_BUILDER_NETWORK_ERROR"
  | "AGENT_BUILDER_RESPONSE_INVALID"
  | "AGENT_DESIGN_SLUG_CONFLICT"
  | "AGENT_DESIGN_CONFLICT_UNRESOLVED"
  | "AGENT_DESIGN_SESSION_LIMIT_EXCEEDED"
  | "AGENT_DESIGN_SECRET_DETECTED";

export class AgentBuilderApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: AgentBuilderApiErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "AgentBuilderApiError";
  }
}

function baseURL(projectId: string) {
  return `${getBackendBaseURL()}/api/projects/${uuidSchema.parse(projectId)}/agent-builder/sessions`;
}

function sessionURL(projectId: string, sessionId: string) {
  return `${baseURL(projectId)}/${uuidSchema.parse(sessionId)}`;
}

function contractURL(url: string, input?: URLSearchParams) {
  const query = new URLSearchParams(input);
  query.set("contract_version", AGENT_BUILDER_CONTRACT_VERSION);
  return `${url}?${query.toString()}`;
}

function safeCode(
  status: number,
  serverCode?: string,
): AgentBuilderApiErrorCode {
  if (
    serverCode === "AGENT_DESIGN_SLUG_CONFLICT" ||
    serverCode === "AGENT_DESIGN_CONFLICT_UNRESOLVED" ||
    serverCode === "AGENT_DESIGN_SESSION_LIMIT_EXCEEDED" ||
    serverCode === "AGENT_DESIGN_SECRET_DETECTED"
  ) {
    return serverCode;
  }
  if (status === 401) return "AUTH_REQUIRED";
  if (status === 403) return "AGENT_BUILDER_FORBIDDEN";
  if (status === 404) return "AGENT_BUILDER_NOT_FOUND";
  if (status === 409) return "AGENT_BUILDER_CONFLICT";
  if (status === 422) return "AGENT_BUILDER_VALIDATION_FAILED";
  return "AGENT_BUILDER_UNAVAILABLE";
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
      throw new AgentBuilderApiError(401, "AUTH_REQUIRED", "需要重新登录");
    }
    if (
      typeof error === "object" &&
      error !== null &&
      "name" in error &&
      error.name === "AbortError"
    ) {
      throw error;
    }
    throw new AgentBuilderApiError(
      0,
      "AGENT_BUILDER_NETWORK_ERROR",
      "无法连接 Agent 设计服务",
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new AgentBuilderApiError(
      response.status,
      "AGENT_BUILDER_RESPONSE_INVALID",
      "Agent 设计服务返回了无效响应",
    );
  }

  if (!response.ok) {
    const error = serverErrorSchema.safeParse(payload);
    const detail = error.success ? error.data.detail : null;
    const message =
      typeof detail === "string"
        ? detail
        : (detail?.message ?? "Agent 设计请求失败");
    throw new AgentBuilderApiError(
      response.status,
      safeCode(
        response.status,
        typeof detail === "string" ? undefined : detail?.code,
      ),
      message,
    );
  }

  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new AgentBuilderApiError(
      response.status,
      "AGENT_BUILDER_RESPONSE_INVALID",
      "Agent 设计服务返回了无效响应",
    );
  }
  return parsed.data;
}

export function createAgentBuilderSession(
  projectId: string,
  input: CreateAgentBuilderSessionInput,
  signal?: AbortSignal,
) {
  const body = createAgentBuilderSessionInputSchema.parse(input);
  return request(
    contractURL(baseURL(projectId)),
    agentBuilderSessionResponseSchema,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}

export function listAgentBuilderSessions(
  projectId: string,
  input: AgentBuilderSessionListInput = {},
  signal?: AbortSignal,
) {
  const parsed = agentBuilderSessionListInputSchema.parse(input);
  const query = new URLSearchParams();
  if (parsed.limit !== undefined) query.set("limit", String(parsed.limit));
  if (parsed.cursor !== undefined) query.set("cursor", parsed.cursor);
  return request(
    contractURL(baseURL(projectId), query),
    agentBuilderSessionListResponseSchema,
    {
      signal,
    },
  );
}

export async function listAllAgentBuilderSessions(
  projectId: string,
  signal?: AbortSignal,
): Promise<AgentBuilderSessionSummary[]> {
  const sessions: AgentBuilderSessionSummary[] = [];
  const seenSessionIds = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | undefined;
  for (
    let pageNumber = 0;
    pageNumber < MAX_AGENT_BUILDER_SESSION_PAGES;
    pageNumber += 1
  ) {
    const page = await listAgentBuilderSessions(
      projectId,
      { limit: 100, ...(cursor ? { cursor } : {}) },
      signal,
    );
    for (const session of page.data) {
      if (seenSessionIds.has(session.id)) {
        throw new AgentBuilderApiError(
          200,
          "AGENT_BUILDER_RESPONSE_INVALID",
          "Agent 设计服务返回了重复的设计会话",
        );
      }
      seenSessionIds.add(session.id);
      sessions.push(session);
    }
    if (page.next_cursor === null) {
      return sortAgentBuilderSessionsForResume(sessions);
    }
    if (seenCursors.has(page.next_cursor)) {
      throw new AgentBuilderApiError(
        200,
        "AGENT_BUILDER_RESPONSE_INVALID",
        "Agent 设计服务返回了重复分页游标",
      );
    }
    seenCursors.add(page.next_cursor);
    cursor = page.next_cursor;
  }
  throw new AgentBuilderApiError(
    200,
    "AGENT_BUILDER_RESPONSE_INVALID",
    "Agent 设计会话分页超过安全上限",
  );
}

export function getAgentBuilderSession(
  projectId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  return request(
    contractURL(sessionURL(projectId, sessionId)),
    agentBuilderSessionResponseSchema,
    { signal },
  );
}

export function submitAgentBuilderTurn(
  projectId: string,
  sessionId: string,
  input: AgentBuilderTurnInput,
  signal?: AbortSignal,
) {
  const body = agentBuilderTurnInputSchema.parse(input);
  return request(
    contractURL(`${sessionURL(projectId, sessionId)}/turns`),
    agentBuilderSessionResponseSchema,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}

export function finalizeAgentBuilderSession(
  projectId: string,
  sessionId: string,
  input: CommitAgentBuilderSessionInput,
  signal?: AbortSignal,
) {
  const body = commitAgentBuilderSessionInputSchema.parse(input);
  return request(
    contractURL(`${sessionURL(projectId, sessionId)}/commit`),
    agentBuilderCommitResponseSchema,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}

export function cancelAgentBuilderSession(
  projectId: string,
  sessionId: string,
  input: CancelAgentBuilderSessionInput,
  signal?: AbortSignal,
) {
  const body = cancelAgentBuilderSessionInputSchema.parse(input);
  return request(
    contractURL(`${sessionURL(projectId, sessionId)}/cancel`),
    agentBuilderSessionResponseSchema,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
}
