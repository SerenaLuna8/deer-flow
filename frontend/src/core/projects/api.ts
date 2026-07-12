import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import { normalizeProjectFilters } from "./query-keys";
import {
  createProjectSchema,
  patchProjectSchema,
  pinProjectSchema,
  projectIdSchema,
  projectPageSchema,
  projectSchema,
  type CreateProjectInput,
  type PatchProjectInput,
  type Project,
  type ProjectErrorCode,
  type ProjectFilters,
  type ProjectPage,
} from "./types";

const serverErrorCodeSchema = z.enum([
  "PROJECT_NOT_FOUND",
  "PROJECT_FORBIDDEN",
  "PROJECT_SLUG_CONFLICT",
  "PROJECT_VALIDATION_FAILED",
  "DATABASE_UNAVAILABLE",
]);

const errorEnvelopeSchema = z
  .object({
    detail: z
      .object({
        code: serverErrorCodeSchema,
        message: z.string().min(1),
      })
      .strict(),
  })
  .strict();

const SAFE_SERVER_MESSAGES: Record<
  z.infer<typeof serverErrorCodeSchema>,
  string
> = {
  PROJECT_NOT_FOUND: "Project not found",
  PROJECT_FORBIDDEN: "Project capability required",
  PROJECT_SLUG_CONFLICT: "Project slug already exists",
  PROJECT_VALIDATION_FAILED: "Project validation failed",
  DATABASE_UNAVAILABLE: "Project storage unavailable",
};

export class ProjectApiError extends Error {
  readonly status: number;
  readonly code: ProjectErrorCode;

  constructor(status: number, code: ProjectErrorCode, message: string) {
    super(message);
    this.name = "ProjectApiError";
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
    throw new ProjectApiError(
      response.status,
      response.ok
        ? "PROJECT_RESPONSE_INVALID"
        : "PROJECT_ERROR_RESPONSE_INVALID",
      response.ok ? "Project response was invalid" : "Project request failed",
    );
  }
}

async function request(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (error instanceof ProjectApiError || isAbortError(error)) throw error;
    if (error instanceof AuthRequiredError) {
      throw new ProjectApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new ProjectApiError(
      0,
      "PROJECT_NETWORK_ERROR",
      "Project service is unavailable",
    );
  }
}

function parseInput<T>(schema: z.ZodType<T>, value: unknown): T {
  const parsed = schema.safeParse(value);
  if (!parsed.success) {
    throw new ProjectApiError(
      422,
      "PROJECT_VALIDATION_FAILED",
      "Project validation failed",
    );
  }
  return parsed.data;
}

async function parseProjectResponse(
  response: Response,
  expectedProjectId?: string,
): Promise<Project> {
  if (!response.ok) await throwResponseError(response);
  const parsed = projectSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new ProjectApiError(
      response.status,
      "PROJECT_RESPONSE_INVALID",
      "Project response was invalid",
    );
  }
  if (expectedProjectId !== undefined && parsed.data.id !== expectedProjectId) {
    throw new ProjectApiError(
      response.status,
      "PROJECT_RESPONSE_INVALID",
      "Project response was invalid",
    );
  }
  return parsed.data;
}

async function throwResponseError(response: Response): Promise<never> {
  const parsed = errorEnvelopeSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new ProjectApiError(
      response.status,
      "PROJECT_ERROR_RESPONSE_INVALID",
      "Project request failed",
    );
  }
  const { code } = parsed.data.detail;
  throw new ProjectApiError(response.status, code, SAFE_SERVER_MESSAGES[code]);
}

function projectUrl(path = ""): string {
  return `${getBackendBaseURL()}/api/projects${path}`;
}

export async function listProjects(
  filters: ProjectFilters = {},
  signal?: AbortSignal,
): Promise<ProjectPage> {
  let normalized;
  try {
    normalized = normalizeProjectFilters(filters);
  } catch (error) {
    if (error instanceof z.ZodError) {
      throw new ProjectApiError(
        422,
        "PROJECT_VALIDATION_FAILED",
        "Project validation failed",
      );
    }
    throw error;
  }
  const params = new URLSearchParams();
  if (normalized.query !== null) params.set("query", normalized.query);
  if (normalized.pinned !== null)
    params.set("pinned", String(normalized.pinned));
  if (normalized.cursor !== null) params.set("cursor", normalized.cursor);
  if (normalized.limit !== null) params.set("limit", String(normalized.limit));
  const query = params.toString();
  const response = await request(projectUrl(query ? `?${query}` : ""), {
    signal,
  });
  if (!response.ok) await throwResponseError(response);
  const parsed = projectPageSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new ProjectApiError(
      response.status,
      "PROJECT_RESPONSE_INVALID",
      "Project response was invalid",
    );
  }
  return parsed.data;
}

export async function createProject(
  input: CreateProjectInput,
  signal?: AbortSignal,
): Promise<Project> {
  const body = parseInput(createProjectSchema, input);
  return parseProjectResponse(
    await request(projectUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    }),
  );
}

export async function getProject(
  projectId: string,
  signal?: AbortSignal,
): Promise<Project> {
  const id = parseInput(projectIdSchema, projectId);
  return parseProjectResponse(
    await request(projectUrl(`/${encodeURIComponent(id)}`), { signal }),
    id,
  );
}

export async function updateProject(
  projectId: string,
  input: PatchProjectInput,
  signal?: AbortSignal,
): Promise<Project> {
  const id = parseInput(projectIdSchema, projectId);
  const body = parseInput(patchProjectSchema, input);
  return parseProjectResponse(
    await request(projectUrl(`/${encodeURIComponent(id)}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    }),
    id,
  );
}

export async function enterProject(
  projectId: string,
  signal?: AbortSignal,
): Promise<Project> {
  const id = parseInput(projectIdSchema, projectId);
  return parseProjectResponse(
    await request(projectUrl(`/${encodeURIComponent(id)}/enter`), {
      method: "POST",
      signal,
    }),
    id,
  );
}

export async function pinProject(
  projectId: string,
  pinned: boolean,
  signal?: AbortSignal,
): Promise<Project> {
  const id = parseInput(projectIdSchema, projectId);
  const body = parseInput(pinProjectSchema, { pinned });
  return parseProjectResponse(
    await request(projectUrl(`/${encodeURIComponent(id)}/pin`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    }),
    id,
  );
}
