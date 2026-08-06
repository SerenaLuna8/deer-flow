import { describe, expect, rs, test } from "@rstest/core";
import { useQuery } from "@tanstack/react-query";

rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn(),
  useQuery: rs.fn((options: unknown) => options),
  useQueryClient: rs.fn(),
}));

import { useProjectAssetVersions } from "@/core/shared-assets/hooks";

describe("shared asset hooks", () => {
  test("keeps an unselected agent version query disabled without building an empty key", () => {
    useProjectAssetVersions(
      "11111111-1111-4111-8111-111111111111",
      "22222222-2222-4222-8222-222222222222",
      "agents",
      "",
      false,
    );

    expect(rs.mocked(useQuery)).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: false,
        queryKey: expect.arrayContaining([
          "asset",
          "__unselected__",
          "versions",
        ]),
      }),
    );
  });
});
