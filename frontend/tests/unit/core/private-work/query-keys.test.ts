import { describe, expect, test } from "@rstest/core";

import {
  privateWorkQueryKey,
  privateWorkRoot,
} from "@/core/private-work/query-keys";

const scope = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};

describe("project private-work query keys", () => {
  test("prefixes every key with account and project identity", () => {
    expect(privateWorkRoot(scope)).toEqual([
      "account",
      scope.accountId,
      "project",
      scope.projectId,
      "private-work",
    ]);
    expect(privateWorkQueryKey(scope, "threads", "search")).toEqual([
      ...privateWorkRoot(scope),
      "threads",
      "search",
    ]);
  });

  test("does not include credentials or mutable project labels", () => {
    expect(JSON.stringify(privateWorkRoot(scope))).not.toMatch(
      /token|secret|slug|name/i,
    );
  });
});
