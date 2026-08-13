import { describe, expect, test } from "@rstest/core";

import {
  INITIAL_ADMIN_CURSOR_STATE,
  advanceAdminCursor,
  retreatAdminCursor,
} from "@/components/admin/ui/admin-page";
import { adminJobsQueryKey } from "@/core/admin-operations/query-keys";
import {
  ADMIN_JOB_PAGE_SIZES,
  DEFAULT_ADMIN_JOB_PAGE_SIZE,
  adminJobPageSizeSchema,
} from "@/core/admin-operations/types";

describe("admin cursor helpers", () => {
  test("advances and retreats cursor history", () => {
    const next = advanceAdminCursor(INITIAL_ADMIN_CURSOR_STATE, "cursor-2");
    expect(next).toEqual({
      cursor: "cursor-2",
      history: [null],
    });
    expect(retreatAdminCursor(next)).toEqual(INITIAL_ADMIN_CURSOR_STATE);
  });
});

describe("admin job page size", () => {
  test("accepts supported page sizes and pins them in the query key", () => {
    for (const pageSize of ADMIN_JOB_PAGE_SIZES) {
      expect(adminJobPageSizeSchema.parse(pageSize)).toBe(pageSize);
    }
    expect(DEFAULT_ADMIN_JOB_PAGE_SIZE).toBe(20);
    expect(
      adminJobsQueryKey(
        "11111111-1111-4111-8111-111111111111",
        null,
        {},
        10,
      ).at(-1),
    ).toBe(10);
  });
});
