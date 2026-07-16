import { describe, expect, test } from "@rstest/core";

import { projectStreamCursorStorageKey } from "@/core/private-work/api-client";

describe("M6 private stream cursor cache isolation", () => {
  test("includes account, project, and thread in every cursor key", () => {
    const first = projectStreamCursorStorageKey(
      {
        accountId: "11111111-1111-4111-8111-111111111111",
        projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      },
      "thread-1",
    );
    const otherAccount = projectStreamCursorStorageKey(
      {
        accountId: "22222222-2222-4222-8222-222222222222",
        projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      },
      "thread-1",
    );
    const otherProject = projectStreamCursorStorageKey(
      {
        accountId: "11111111-1111-4111-8111-111111111111",
        projectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      },
      "thread-1",
    );
    const otherThread = projectStreamCursorStorageKey(
      {
        accountId: "11111111-1111-4111-8111-111111111111",
        projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      },
      "thread-2",
    );

    expect(new Set([first, otherAccount, otherProject, otherThread]).size).toBe(
      4,
    );
  });
});
