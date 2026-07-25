import { describe, expect, test, rs } from "@rstest/core";

import {
  ProjectApiError,
  findProjectBySlug,
  listAllProjects,
} from "@/core/projects/api";
import { shouldRetryProjectSlugResolution } from "@/core/projects/hooks";
import { CAPABILITIES, type ProjectPage } from "@/core/projects/types";

const project = (slug: string, id: string) => ({
  id,
  slug,
  display_name: slug,
  description: "",
  icon: "folder",
  role: "admin" as const,
  capabilities: [...CAPABILITIES],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 1, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active" as const,
  is_suspended: false,
  membership_version: 1,
  request_id: "trace",
});

describe("project slug resolution", () => {
  test("paginates search results and returns only an exact slug match", async () => {
    const pages: ProjectPage[] = [
      {
        items: [
          project("alpha-project-copy", "11111111-1111-4111-8111-111111111111"),
        ],
        next_cursor: "page-2",
      },
      {
        items: [
          project("alpha-project", "22222222-2222-4222-8222-222222222222"),
        ],
        next_cursor: null,
      },
    ];
    const list = rs.fn((_filters, _signal) => Promise.resolve(pages.shift()!));
    const signal = new AbortController().signal;
    await expect(
      findProjectBySlug("alpha-project", signal, list),
    ).resolves.toMatchObject({
      slug: "alpha-project",
      id: "22222222-2222-4222-8222-222222222222",
    });
    expect(list.mock.calls).toEqual([
      [{ query: "alpha-project", limit: 100 }, signal],
      [{ query: "alpha-project", limit: 100, cursor: "page-2" }, signal],
    ]);
  });

  test("fails safely on cursor loops and exact-slug misses", async () => {
    const loop = rs.fn(() =>
      Promise.resolve({ items: [], next_cursor: "same-cursor" }),
    );
    await expect(
      findProjectBySlug("missing", undefined, loop),
    ).rejects.toMatchObject({
      code: "PROJECT_RESPONSE_INVALID",
    });

    const empty = rs.fn(() =>
      Promise.resolve({ items: [], next_cursor: null }),
    );
    const missing = await findProjectBySlug("missing", undefined, empty).catch(
      (error: unknown) => error,
    );
    expect(missing).toBeInstanceOf(ProjectApiError);
    expect(missing).toMatchObject({ status: 404, code: "PROJECT_NOT_FOUND" });
  });

  test("forwards abort without converting it to not-found", async () => {
    const aborted = new DOMException("Aborted", "AbortError");
    const list = rs.fn(() => Promise.reject(aborted));
    await expect(findProjectBySlug("alpha", undefined, list)).rejects.toBe(
      aborted,
    );
  });

  test("does not retry a member-scoped slug miss but keeps bounded transient retries", () => {
    const unavailable = new ProjectApiError(
      404,
      "PROJECT_NOT_FOUND",
      "Project not found",
    );
    expect(shouldRetryProjectSlugResolution(0, unavailable)).toBe(false);

    const transient = new ProjectApiError(
      503,
      "DATABASE_UNAVAILABLE",
      "Project storage unavailable",
    );
    expect(shouldRetryProjectSlugResolution(0, transient)).toBe(true);
    expect(shouldRetryProjectSlugResolution(2, transient)).toBe(true);
    expect(shouldRetryProjectSlugResolution(3, transient)).toBe(false);
  });
});

describe("project list pagination", () => {
  test("aggregates every page and forwards one abort signal", async () => {
    const signal = new AbortController().signal;
    const pinned = {
      ...project("second-page", "33333333-3333-4333-8333-333333333333"),
      is_pinned: true,
    };
    const list = rs
      .fn()
      .mockResolvedValueOnce({
        items: Array.from({ length: 100 }, (_, index) =>
          project(
            `project-${index}`,
            `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
          ),
        ),
        next_cursor: "page-2",
      })
      .mockResolvedValueOnce({ items: [pinned], next_cursor: null });

    const page = await listAllProjects({ query: "project" }, signal, list);
    expect(page.items).toHaveLength(101);
    expect(page.items.at(-1)).toEqual(pinned);
    expect(page.next_cursor).toBeNull();
    expect(list.mock.calls).toEqual([
      [{ query: "project", limit: 100 }, signal],
      [{ query: "project", limit: 100, cursor: "page-2" }, signal],
    ]);
  });

  test("fails closed when the all-project cursor loops", async () => {
    const list = rs.fn(() =>
      Promise.resolve({ items: [], next_cursor: "same-cursor" }),
    );
    await expect(listAllProjects({}, undefined, list)).rejects.toMatchObject({
      code: "PROJECT_RESPONSE_INVALID",
    });
  });
});
