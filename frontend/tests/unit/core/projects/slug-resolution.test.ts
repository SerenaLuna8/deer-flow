import { describe, expect, test, rs } from "@rstest/core";

import { ProjectApiError, findProjectBySlug } from "@/core/projects/api";
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
});
