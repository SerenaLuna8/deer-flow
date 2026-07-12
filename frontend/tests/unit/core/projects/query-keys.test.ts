import { describe, expect, test } from "@rstest/core";

import {
  accountProjectsKey,
  normalizeProjectFilters,
  projectDetailKey,
  projectKeys,
} from "@/core/projects/query-keys";

describe("project query keys", () => {
  test("scopes list and detail keys by account", () => {
    expect(
      accountProjectsKey("u1", { query: " alpha ", pinned: true }),
    ).toEqual([
      "account",
      "u1",
      "projects",
      {
        query: "alpha",
        pinned: true,
        cursor: null,
        limit: null,
        includeRecoverable: false,
      },
    ]);
    expect(projectDetailKey("u1", "p1")).toEqual([
      "account",
      "u1",
      "project",
      "p1",
      "detail",
    ]);
    expect(projectKeys.lists("u1")).toEqual(["account", "u1", "projects"]);
    expect(projectDetailKey("u1", "p1")).not.toEqual(
      projectDetailKey("u2", "p1"),
    );
  });

  test("normalizes equivalent filters without undefined or mutable references", () => {
    const input = { query: " alpha ", pinned: undefined, limit: 20 };
    const first = normalizeProjectFilters(input);
    input.query = "changed";
    const second = normalizeProjectFilters({ query: "alpha", limit: 20 });
    expect(first).toEqual(second);
    expect(first).toEqual({
      query: "alpha",
      pinned: null,
      cursor: null,
      limit: 20,
      includeRecoverable: false,
    });
    expect(Object.values(first)).not.toContain(undefined);
    expect(Object.isFrozen(first)).toBe(true);
  });
});
