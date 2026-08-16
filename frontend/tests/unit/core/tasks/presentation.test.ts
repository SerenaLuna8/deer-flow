import { describe, expect, test } from "@rstest/core";

import type { Model } from "@/core/models/types";
import { resolveSubtaskModelLabel } from "@/core/tasks/presentation";

const MODEL_REF = "00000000-0000-4000-8000-000000000401";

const model: Model = {
  name: MODEL_REF,
  model: MODEL_REF,
  display_name: "Visible model",
  supports_thinking: true,
  supports_reasoning_effort: true,
  supports_vision: true,
  supports_vision_bridge: false,
  is_default: true,
};

describe("resolveSubtaskModelLabel", () => {
  test("uses the catalog display name", () => {
    expect(resolveSubtaskModelLabel(MODEL_REF, [model])).toBe("Visible model");
  });

  test("does not expose an unavailable internal model reference", () => {
    expect(
      resolveSubtaskModelLabel("00000000-0000-4000-8000-000000000499", [model]),
    ).toBeUndefined();
  });
});
