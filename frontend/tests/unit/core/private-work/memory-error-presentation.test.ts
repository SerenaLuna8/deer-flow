import { describe, expect, test } from "@rstest/core";

import { GatewayApiError } from "@/core/api/errors";
import { memoryDreamErrorMessage } from "@/core/private-work/memory/error-presentation";

const copy = {
  dreamFailed: "localized generic failure",
  dreamModelUnavailable: "localized model unavailable",
};

describe("Memory Dream error presentation", () => {
  test("localizes the stable model-unavailable code instead of exposing backend text", () => {
    expect(
      memoryDreamErrorMessage(
        new GatewayApiError(
          409,
          "MEMORY_DREAM_MODEL_UNAVAILABLE",
          "backend English detail",
        ),
        copy,
      ),
    ).toBe("localized model unavailable");
  });

  test("preserves existing detail and fallback behavior for unrelated failures", () => {
    expect(
      memoryDreamErrorMessage(
        new GatewayApiError(409, "MEMORY_CONFLICT", "Memory changed"),
        copy,
      ),
    ).toBe("Memory changed");
    expect(memoryDreamErrorMessage(null, copy)).toBe(
      "localized generic failure",
    );
  });
});
