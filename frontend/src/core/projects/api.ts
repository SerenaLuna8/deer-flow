import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import { normalizeProjectFilters } from "./query-keys";
import {
  changeProjectMemberRoleSchema,
  createProjectInvitationSchema,
  createProjectSchema,
  createdProjectInvitationSchema,
  invitationClaimResponseSchema,
  invitationClaimSchema,
  membershipVersionSchema,
  patchProjectSchema,
  pinProjectSchema,
  projectInvitationListSchema,
  projectInvitationSchema,
  projectIdSchema,
  projectMembershipListSchema,
  projectMembershipSchema,
  projectPageSchema,
  projectSchema,
  redeemedProjectInvitationSchema,
  type ChangeProjectMemberRoleInput,
  type CreateProjectInput,
  type CreateProjectInvitationInput,
  type CreatedProjectInvitation,
  type PatchProjectInput,
  type Project,
  type ProjectErrorCode,
  type ProjectFilters,
  type ProjectInvitation,
  type ProjectMembership,
  type ProjectPage,
  type RedeemedProjectInvitation,
} from "./types";

const serverErrorCodeSchema = z.enum([
  "PROJECT_NOT_FOUND",
  "PROJECT_FORBIDDEN",
  "PROJECT_OR_MEMBER_NOT_FOUND",
  "PROJECT_MEMBERSHIP_FORBIDDEN",
  "PROJECT_LAST_ADMIN",
  "PROJECT_MEMBER_QUOTA_EXCEEDED",
  "PROJECT_MEMBERSHIP_VERSION_CONFLICT",
  "PROJECT_QUOTA_STATE_CONFLICT",
  "PROJECT_INVITATION_CONFLICT",
  "PROJECT_INVITATION_INVALID",
  "PROJECT_DELETION_STATE_CONFLICT",
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
        request_id: z.string().min(1).optional(),
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
  PROJECT_OR_MEMBER_NOT_FOUND: "Project or member not found",
  PROJECT_MEMBERSHIP_FORBIDDEN:
    "Project membership does not allow this operation",
  PROJECT_LAST_ADMIN: "Project must keep an active admin",
  PROJECT_MEMBER_QUOTA_EXCEEDED: "Project member quota was exceeded",
  PROJECT_MEMBERSHIP_VERSION_CONFLICT: "Project membership version conflict",
  PROJECT_QUOTA_STATE_CONFLICT: "Project quota state conflict",
  PROJECT_INVITATION_CONFLICT: "Project invitation conflict",
  PROJECT_INVITATION_INVALID: "Project invitation is invalid",
  PROJECT_DELETION_STATE_CONFLICT: "Project deletion state conflict",
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

async function parseResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
): Promise<T> {
  if (!response.ok) await throwResponseError(response);
  const parsed = schema.safeParse(await readJson(response));
  if (!parsed.success) {
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
  if (normalized.includeRecoverable) params.set("include_recoverable", "true");
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

export type ProjectListFunction = typeof listProjects;

export async function listAllProjects(
  filters: ProjectFilters = {},
  signal?: AbortSignal,
  list: ProjectListFunction = listProjects,
): Promise<ProjectPage> {
  const items: Project[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | undefined;
  while (true) {
    const page = await list(
      {
        ...filters,
        limit: 100,
        ...(cursor ? { cursor } : {}),
      },
      signal,
    );
    items.push(...page.items);
    if (page.next_cursor === null) return { items, next_cursor: null };
    if (seenCursors.has(page.next_cursor)) {
      throw new ProjectApiError(
        502,
        "PROJECT_RESPONSE_INVALID",
        "Project response was invalid",
      );
    }
    seenCursors.add(page.next_cursor);
    cursor = page.next_cursor;
  }
}

export async function findProjectBySlug(
  slug: string,
  signal?: AbortSignal,
  list: ProjectListFunction = listProjects,
): Promise<Project> {
  const normalizedSlug = slug.trim();
  if (!normalizedSlug) {
    throw new ProjectApiError(
      422,
      "PROJECT_VALIDATION_FAILED",
      "Project validation failed",
    );
  }
  const seenCursors = new Set<string>();
  let cursor: string | undefined;
  while (true) {
    const filters: ProjectFilters = {
      query: normalizedSlug,
      limit: 100,
      ...(cursor ? { cursor } : {}),
    };
    const page = await list(filters, signal);
    const exact = page.items.find((project) => project.slug === normalizedSlug);
    if (exact) return exact;
    if (page.next_cursor === null) {
      throw new ProjectApiError(404, "PROJECT_NOT_FOUND", "Project not found");
    }
    if (seenCursors.has(page.next_cursor)) {
      throw new ProjectApiError(
        502,
        "PROJECT_RESPONSE_INVALID",
        "Project response was invalid",
      );
    }
    seenCursors.add(page.next_cursor);
    cursor = page.next_cursor;
  }
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

function projectGovernanceUrl(projectId: string, path = ""): string {
  const id = parseInput(projectIdSchema, projectId);
  return projectUrl(`/${encodeURIComponent(id)}${path}`);
}

function jsonRequest(
  method: "POST" | "PATCH" | "DELETE",
  body: unknown,
  signal?: AbortSignal,
): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  };
}

export async function listProjectMembers(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectMembership[]> {
  return parseResponse(
    await request(projectGovernanceUrl(projectId, "/members"), { signal }),
    projectMembershipListSchema,
  );
}

export async function changeProjectMemberRole(
  projectId: string,
  membershipId: string,
  input: ChangeProjectMemberRoleInput,
  signal?: AbortSignal,
): Promise<ProjectMembership> {
  const memberId = parseInput(projectIdSchema, membershipId);
  const body = parseInput(changeProjectMemberRoleSchema, input);
  return parseResponse(
    await request(
      projectGovernanceUrl(
        projectId,
        `/members/${encodeURIComponent(memberId)}`,
      ),
      jsonRequest("PATCH", body, signal),
    ),
    projectMembershipSchema,
  );
}

export async function removeProjectMember(
  projectId: string,
  membershipId: string,
  version: number,
  signal?: AbortSignal,
): Promise<ProjectMembership> {
  const memberId = parseInput(projectIdSchema, membershipId);
  const body = parseInput(membershipVersionSchema, { version });
  return parseResponse(
    await request(
      projectGovernanceUrl(
        projectId,
        `/members/${encodeURIComponent(memberId)}`,
      ),
      jsonRequest("DELETE", body, signal),
    ),
    projectMembershipSchema,
  );
}

export async function leaveProject(
  projectId: string,
  version: number,
  signal?: AbortSignal,
): Promise<ProjectMembership> {
  const body = parseInput(membershipVersionSchema, { version });
  return parseResponse(
    await request(
      projectGovernanceUrl(projectId, "/leave"),
      jsonRequest("POST", body, signal),
    ),
    projectMembershipSchema,
  );
}

function projectInvitationsUrl(path = ""): string {
  return `${getBackendBaseURL()}/api/project-invitations${path}`;
}

export async function listMyProjectInvitations(
  signal?: AbortSignal,
): Promise<ProjectInvitation[]> {
  return parseResponse(
    await request(projectInvitationsUrl("/mine"), { signal }),
    projectInvitationListSchema,
  );
}

export async function listProjectInvitations(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectInvitation[]> {
  return parseResponse(
    await request(projectGovernanceUrl(projectId, "/invitations"), { signal }),
    projectInvitationListSchema,
  );
}

export async function createProjectInvitation(
  projectId: string,
  input: CreateProjectInvitationInput,
  signal?: AbortSignal,
): Promise<CreatedProjectInvitation> {
  const body = parseInput(createProjectInvitationSchema, input);
  return parseResponse(
    await request(
      projectGovernanceUrl(projectId, "/invitations"),
      jsonRequest("POST", body, signal),
    ),
    createdProjectInvitationSchema,
  );
}

export async function revokeProjectInvitation(
  projectId: string,
  invitationId: string,
  version: number,
  signal?: AbortSignal,
): Promise<ProjectInvitation> {
  const id = parseInput(projectIdSchema, invitationId);
  const body = parseInput(membershipVersionSchema, { version });
  return parseResponse(
    await request(
      projectGovernanceUrl(projectId, `/invitations/${encodeURIComponent(id)}`),
      jsonRequest("DELETE", body, signal),
    ),
    projectInvitationSchema,
  );
}

export async function claimProjectInvitation(
  token: string,
  signal?: AbortSignal,
): Promise<{ message: "Invitation claim processed" }> {
  const body = parseInput(invitationClaimSchema, { token });
  return parseResponse(
    await request(
      projectInvitationsUrl("/claim"),
      jsonRequest("POST", body, signal),
    ),
    invitationClaimResponseSchema,
  );
}

export async function redeemProjectInvitation(
  signal?: AbortSignal,
): Promise<RedeemedProjectInvitation> {
  return parseResponse(
    await request(projectInvitationsUrl("/redeem"), {
      method: "POST",
      signal,
    }),
    redeemedProjectInvitationSchema,
  );
}

export async function requestProjectDeletion(
  projectId: string,
  signal?: AbortSignal,
): Promise<Project> {
  return parseProjectResponse(
    await request(projectGovernanceUrl(projectId, "/deletion"), {
      method: "POST",
      signal,
    }),
    projectId,
  );
}

export async function restoreProject(
  projectId: string,
  signal?: AbortSignal,
): Promise<Project> {
  return parseProjectResponse(
    await request(projectGovernanceUrl(projectId, "/restore"), {
      method: "POST",
      signal,
    }),
    projectId,
  );
}
