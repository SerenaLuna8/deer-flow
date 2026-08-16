import { describe, expect, test } from "@rstest/core";

import { resolveModelDisplayName } from "@/core/models/presentation";
import type { Model } from "@/core/models/types";

const MODEL_REF = "00000000-0000-4000-8000-000000000401";

const models: Model[] = [
  {
    name: MODEL_REF,
    model: MODEL_REF,
    display_name: "Visible model",
    supports_thinking: true,
    supports_reasoning_effort: true,
    supports_vision: true,
    supports_vision_bridge: false,
    is_default: true,
  },
];

describe("resolveModelDisplayName", () => {
  test("resolves exact and default references through the public catalog", () => {
    expect(resolveModelDisplayName(MODEL_REF, models)).toBe("Visible model");
    expect(resolveModelDisplayName("default", models)).toBe("Visible model");
  });

  test("does not expose an unknown reference", () => {
    expect(
      resolveModelDisplayName("00000000-0000-4000-8000-000000000499", models),
    ).toBeUndefined();
  });
});
