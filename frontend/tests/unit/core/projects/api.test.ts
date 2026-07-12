import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  ProjectApiError,
  createProject,
  enterProject,
  getProject,
  listProjects,
  pinProject,
  updateProject,
} from "@/core/projects/api";
import { CAPABILITIES } from "@/core/projects/types";

const mockedFetch = rs.mocked(fetchWithAuth);
const project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha-project",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: [...CAPABILITIES],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace-1",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("projects api", () => {
  test("encodes stable list filters and forwards AbortSignal", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, { items: [project], next_cursor: "next" }),
    );
    const controller = new AbortController();
    await expect(
      listProjects(
        {
          query: " alpha & beta ",
          pinned: false,
          cursor: "cursor+/=",
          limit: 25,
        },
        controller.signal,
      ),
    ).resolves.toMatchObject({ next_cursor: "next" });
    expect(mockedFetch).toHaveBeenCalledWith(
      "/backend/api/projects?query=alpha+%26+beta&pinned=false&cursor=cursor%2B%2F%3D&limit=25",
      { signal: controller.signal },
    );
  });

  test("uses the CSRF fetch wrapper for every project method without identity fields", async () => {
    mockedFetch.mockImplementation(() =>
      Promise.resolve(jsonResponse(200, project)),
    );
    const signal = new AbortController().signal;
    await createProject(
      { slug: "alpha-project", display_name: "Alpha" },
      signal,
    );
    await getProject(project.id, signal);
    await updateProject(project.id, { display_name: "Changed" }, signal);
    await enterProject(project.id, signal);
    await pinProject(project.id, true, signal);

    expect(mockedFetch.mock.calls).toEqual([
      [
        "/backend/api/projects",
        expect.objectContaining({ method: "POST", signal }),
      ],
      [`/backend/api/projects/${project.id}`, { signal }],
      [
        `/backend/api/projects/${project.id}`,
        expect.objectContaining({ method: "PATCH", signal }),
      ],
      [
        `/backend/api/projects/${project.id}/enter`,
        expect.objectContaining({ method: "POST", signal }),
      ],
      [
        `/backend/api/projects/${project.id}/pin`,
        expect.objectContaining({ method: "PUT", signal }),
      ],
    ]);
    expect(mockedFetch.mock.calls[0]?.[1]).toMatchObject({
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: "alpha-project", display_name: "Alpha" }),
    });
    expect(mockedFetch.mock.calls[2]?.[1]).toMatchObject({
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: "Changed" }),
    });
    expect(mockedFetch.mock.calls[4]?.[1]).toMatchObject({
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned: true }),
    });
    for (const [, init] of mockedFetch.mock.calls) {
      const body = typeof init?.body === "string" ? init.body : "";
      expect(body).not.toContain("user_id");
      expect(body).not.toContain("capabilities");
      expect(body).not.toContain('"role"');
    }
  });

  test("preserves safe public errors including slug conflict", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: {
          code: "PROJECT_SLUG_CONFLICT",
          message: "Project slug already exists",
        },
      }),
    );
    const error = await createProject({
      slug: "alpha-project",
      display_name: "Alpha",
    }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ProjectApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "PROJECT_SLUG_CONFLICT",
      message: "Project slug already exists",
    });
  });

  test("maps network, non-json, and schema drift without exposing raw payloads", async () => {
    mockedFetch.mockRejectedValueOnce(
      new Error("postgresql://owner:secret@db/private"),
    );
    const networkError = await listProjects({}).catch(
      (caught: unknown) => caught,
    );
    expect(networkError).toMatchObject({
      code: "PROJECT_NETWORK_ERROR",
      message: "Project service is unavailable",
    });
    expect(networkError).not.toHaveProperty("cause");

    mockedFetch.mockResolvedValueOnce(
      new Response("<html>proxy failed secret sql</html>", { status: 503 }),
    );
    await expect(listProjects({})).rejects.toMatchObject({
      code: "PROJECT_ERROR_RESPONSE_INVALID",
      message: "Project request failed",
    });

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, { ...project, private_owner: "secret" }),
    );
    await expect(getProject(project.id)).rejects.toMatchObject({
      code: "PROJECT_RESPONSE_INVALID",
      message: "Project response was invalid",
    });
  });

  test("does not trust unknown error codes or malicious backend messages", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(500, {
        detail: {
          code: "UNKNOWN_DATABASE_ERROR",
          message:
            "SELECT secret FROM users postgresql://owner:password@db/private",
        },
      }),
    );
    await expect(listProjects({})).rejects.toMatchObject({
      status: 500,
      code: "PROJECT_ERROR_RESPONSE_INVALID",
      message: "Project request failed",
    });
  });

  test("preserves abort cancellation", async () => {
    const aborted = new DOMException("Aborted", "AbortError");
    mockedFetch.mockRejectedValueOnce(aborted);
    await expect(listProjects({}, new AbortController().signal)).rejects.toBe(
      aborted,
    );
  });

  test("maps the fetcher's explicit authentication error without guessing messages", async () => {
    mockedFetch.mockRejectedValueOnce(new AuthRequiredError());
    await expect(listProjects({})).rejects.toMatchObject({
      status: 401,
      code: "AUTH_REQUIRED",
      message: "Authentication required",
    });
  });

  test("rejects a detail response whose id does not match the requested route", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        ...project,
        id: "22222222-2222-4222-8222-222222222222",
      }),
    );
    await expect(getProject(project.id)).rejects.toMatchObject({
      code: "PROJECT_RESPONSE_INVALID",
      message: "Project response was invalid",
    });
  });

  test("maps invalid input to the project domain before issuing a request", async () => {
    await expect(
      createProject({ slug: "alpha", display_name: "" }),
    ).rejects.toMatchObject({
      status: 422,
      code: "PROJECT_VALIDATION_FAILED",
      message: "Project validation failed",
    });
    await expect(getProject("not-a-uuid")).rejects.toMatchObject({
      code: "PROJECT_VALIDATION_FAILED",
    });
    expect(mockedFetch).not.toHaveBeenCalled();
  });
});
