import { describe, expect, test } from "@rstest/core";

import {
  createAdminModelProviderSettingsDraft,
  resetAdminModelProviderSettingDraftValue,
  serializeAdminModelProviderSettingsDraft,
  updateAdminModelProviderSettingDraftValue,
  type AdminModelProviderAdapterDescriptor,
} from "@/core/admin-settings/models";

const descriptor: AdminModelProviderAdapterDescriptor = {
  id: "typed_provider",
  api_key_required: true,
  setting_fields: [
    {
      name: "base_url",
      label: "Base URL",
      input_type: "url",
      advanced: false,
      form_control: "input",
      default_mode: "platform",
      default_value: "https://provider.example.test/v1",
      minimum: null,
      maximum: null,
      step: null,
      options: [],
    },
    {
      name: "max_tokens",
      label: "Max tokens",
      input_type: "integer",
      advanced: false,
      form_control: "input",
      default_mode: "platform",
      default_value: 51_200,
      minimum: 1,
      maximum: 2_000_000,
      step: 1,
      options: [],
    },
    {
      name: "temperature",
      label: "Temperature",
      input_type: "number",
      advanced: true,
      form_control: "input",
      default_mode: "provider",
      default_value: null,
      minimum: -2,
      maximum: 2,
      step: 0.01,
      options: [],
    },
    {
      name: "streaming_enabled",
      label: "Streaming enabled",
      input_type: "boolean",
      advanced: true,
      form_control: "input",
      default_mode: "provider",
      default_value: null,
      minimum: null,
      maximum: null,
      step: null,
      options: [],
    },
    {
      name: "reasoning_effort",
      label: "Reasoning effort",
      input_type: "enum",
      advanced: true,
      form_control: "input",
      default_mode: "platform",
      default_value: "medium",
      minimum: null,
      maximum: null,
      step: null,
      options: ["none", "medium", "high"],
    },
    {
      name: "routing_label",
      label: "Routing label",
      input_type: "string",
      advanced: true,
      form_control: "input",
      default_mode: "platform",
      default_value: "standard",
      minimum: null,
      maximum: null,
      step: null,
      options: [],
    },
    {
      name: "extra_body",
      label: "Extra request body",
      input_type: "json",
      advanced: true,
      form_control: "preserve",
      default_mode: "provider",
      default_value: null,
      minimum: null,
      maximum: null,
      step: null,
      options: [],
    },
  ],
};

describe("descriptor-driven admin model provider settings", () => {
  test("prefills platform defaults and lets saved scalar values win, including false and zero", () => {
    const draft = createAdminModelProviderSettingsDraft(descriptor, {
      max_tokens: 64_000,
      temperature: 0,
      streaming_enabled: false,
      reasoning_effort: "high",
      extra_body: { reasoning: { effort: "high" } },
    });

    expect(draft.values).toMatchObject({
      base_url: "https://provider.example.test/v1",
      max_tokens: "64000",
      temperature: "0",
      streaming_enabled: "false",
      reasoning_effort: "high",
      routing_label: "standard",
    });
    expect(draft.preserved_settings).toEqual({
      extra_body: { reasoning: { effort: "high" } },
    });
    expect(draft.incompatible_keys).toEqual([]);
  });

  test("omits untouched platform and provider defaults while retaining saved keys exactly", () => {
    const fresh = createAdminModelProviderSettingsDraft(descriptor, {});
    expect(serializeAdminModelProviderSettingsDraft(descriptor, fresh)).toEqual(
      {},
    );

    const saved = createAdminModelProviderSettingsDraft(descriptor, {
      max_tokens: 51_200,
      temperature: 0,
      streaming_enabled: false,
      extra_body: { reasoning_format: "deepseek-style" },
    });
    expect(serializeAdminModelProviderSettingsDraft(descriptor, saved)).toEqual(
      {
        max_tokens: 51_200,
        temperature: 0,
        streaming_enabled: false,
        extra_body: { reasoning_format: "deepseek-style" },
      },
    );
  });

  test("serializes typed modifications and clearing restores the declared default", () => {
    let draft = createAdminModelProviderSettingsDraft(descriptor, {});
    draft = updateAdminModelProviderSettingDraftValue(
      draft,
      "max_tokens",
      "64000",
    );
    draft = updateAdminModelProviderSettingDraftValue(
      draft,
      "temperature",
      "0.25",
    );
    draft = updateAdminModelProviderSettingDraftValue(
      draft,
      "streaming_enabled",
      "true",
    );
    draft = updateAdminModelProviderSettingDraftValue(
      draft,
      "routing_label",
      "  premium  ",
    );
    expect(serializeAdminModelProviderSettingsDraft(descriptor, draft)).toEqual(
      {
        max_tokens: 64_000,
        temperature: 0.25,
        streaming_enabled: true,
        routing_label: "premium",
      },
    );

    draft = resetAdminModelProviderSettingDraftValue(
      descriptor,
      draft,
      "max_tokens",
    );
    draft = resetAdminModelProviderSettingDraftValue(
      descriptor,
      draft,
      "temperature",
    );
    expect(draft.values.max_tokens).toBe("51200");
    expect(draft.values.temperature).toBe("");
    expect(serializeAdminModelProviderSettingsDraft(descriptor, draft)).toEqual(
      {
        streaming_enabled: true,
        routing_label: "premium",
      },
    );
  });

  test("enforces enum, numeric and URL constraints through the provider schema", () => {
    for (const [name, value] of [
      ["max_tokens", "0"],
      ["max_tokens", "1.5"],
      ["max_tokens", "2000001"],
      ["temperature", "Infinity"],
      ["temperature", "0.005"],
      ["temperature", "2.01"],
      ["reasoning_effort", "extreme"],
      ["base_url", "https://user:password@provider.example.test/v1"],
    ] as const) {
      const invalid = updateAdminModelProviderSettingDraftValue(
        createAdminModelProviderSettingsDraft(descriptor, {}),
        name,
        value,
      );
      expect(() =>
        serializeAdminModelProviderSettingsDraft(descriptor, invalid),
      ).toThrow();
    }

    for (const boundary of ["1", "2000000"]) {
      const valid = updateAdminModelProviderSettingDraftValue(
        createAdminModelProviderSettingsDraft(descriptor, {}),
        "max_tokens",
        boundary,
      );
      expect(
        serializeAdminModelProviderSettingsDraft(descriptor, valid).max_tokens,
      ).toBe(Number(boundary));
    }
  });

  test("fails closed for unknown historical keys and never silently drops them", () => {
    const draft = createAdminModelProviderSettingsDraft(descriptor, {
      retired_vendor_flag: true,
    });

    expect(draft.incompatible_keys).toEqual(["retired_vendor_flag"]);
    expect(() =>
      serializeAdminModelProviderSettingsDraft(descriptor, draft),
    ).toThrow("retired_vendor_flag");
  });

  test("fails closed when a known preserved value violates its descriptor", () => {
    const preserveEnum: AdminModelProviderAdapterDescriptor = {
      ...descriptor,
      setting_fields: [
        ...descriptor.setting_fields,
        {
          name: "opaque_mode",
          label: "Opaque mode",
          input_type: "enum",
          advanced: true,
          form_control: "preserve",
          default_mode: "provider",
          default_value: null,
          minimum: null,
          maximum: null,
          step: null,
          options: ["safe"],
        },
      ],
    };

    const draft = createAdminModelProviderSettingsDraft(preserveEnum, {
      opaque_mode: "unsafe",
    });

    expect(draft.preserved_settings).toEqual({});
    expect(draft.incompatible_keys).toEqual(["opaque_mode"]);
    expect(() =>
      serializeAdminModelProviderSettingsDraft(preserveEnum, draft),
    ).toThrow("opaque_mode");
  });
});
