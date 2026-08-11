import { describe, expect, it } from "@rstest/core";

import { modelSchema } from "@/core/models/types";

const MODEL = {
  name: "primary-chat",
  model: "primary-chat",
  display_name: "Primary Chat",
  description: "Safe label",
  supports_thinking: true,
  supports_reasoning_effort: true,
  supports_vision: false,
  is_default: true,
  workflow_authoring: {
    modes: ["chat"],
    supports_streaming: true,
    parameters: [
      {
        name: "temperature",
        kind: "number",
        minimum: -2,
        maximum: 2,
      },
      {
        name: "max_tokens",
        kind: "integer",
        minimum: 1,
        maximum: 2_000_000,
      },
    ],
  },
} as const;

describe("G20 Workflow Model authoring projection", () => {
  it("accepts only the bounded secret-free capability shape", () => {
    const parsed = modelSchema.parse(MODEL);
    expect(parsed.workflow_authoring.modes).toEqual(["chat"]);
    expect(() =>
      (parsed.workflow_authoring.modes as unknown as string[]).push(
        "completion",
      ),
    ).toThrow();
    expect(
      () =>
        ((
          parsed.workflow_authoring.parameters[0] as unknown as Record<
            string,
            unknown
          >
        ).maximum = 99),
    ).toThrow();
    expect(
      modelSchema.safeParse({ ...MODEL, provider_adapter: "openai" }).success,
    ).toBe(false);
    expect(
      modelSchema.safeParse({ ...MODEL, credential_id: "secret-coordinate" })
        .success,
    ).toBe(false);
    expect(
      modelSchema.safeParse({
        ...MODEL,
        workflow_authoring: {
          ...MODEL.workflow_authoring,
          modes: ["chat", "chat"],
        },
      }).success,
    ).toBe(false);
  });

  it("rejects invented parameters, mismatched kinds, and invalid ranges", () => {
    for (const parameter of [
      {
        name: "top_p",
        kind: "number",
        minimum: 0,
        maximum: 1,
      },
      {
        name: "max_tokens",
        kind: "number",
        minimum: 1,
        maximum: 2_000_000,
      },
      {
        name: "temperature",
        kind: "number",
        minimum: -3,
        maximum: 2,
      },
    ]) {
      expect(
        modelSchema.safeParse({
          ...MODEL,
          workflow_authoring: {
            modes: ["chat"],
            supports_streaming: true,
            parameters: [parameter],
          },
        }).success,
      ).toBe(false);
    }

    expect(
      modelSchema.safeParse({
        ...MODEL,
        workflow_authoring: {
          ...MODEL.workflow_authoring,
          modes: ["completion", "chat"],
          parameters: [...MODEL.workflow_authoring.parameters].reverse(),
        },
      }).success,
    ).toBe(false);
  });
});
