import { expect, test } from "@rstest/core";

import {
  agentModeForRunExecutionProfile,
  buildRunExecutionProfileRequest,
  getAgentModeExecutionProfile,
  resolveAgentExecutionAvailability,
  resolveAgentExecutionModelSelection,
  resolveAgentMode,
} from "@/core/threads/agent-mode";

test("ignores a stale non-explicit model when the system default changes", () => {
  const models = [
    {
      name: "deepseek-v4-flash",
      is_default: false,
      supports_thinking: false,
      supports_reasoning_effort: false,
    },
    {
      name: "gpt-5.6-luna",
      is_default: true,
      supports_thinking: true,
      supports_reasoning_effort: true,
    },
  ];

  expect(
    resolveAgentExecutionModelSelection(
      models,
      "deepseek-v4-flash",
      "default",
      false,
    ),
  ).toEqual({
    model: models[1],
    modelName: "gpt-5.6-luna",
    modelSelectionLocked: false,
  });
});

test("maps the server effective execution profile back to the displayed mode", () => {
  expect(
    agentModeForRunExecutionProfile({
      model_name: "gpt-5.6-luna",
      thinking_enabled: true,
      reasoning_effort: "high",
      supports_vision: true,
    }),
  ).toBe("ultra");
  expect(
    agentModeForRunExecutionProfile({
      model_name: "deepseek-v4-flash",
      thinking_enabled: false,
      reasoning_effort: null,
      supports_vision: false,
    }),
  ).toBe("flash");
});

test("keeps all thinking strengths only when the model supports effort", () => {
  expect(resolveAgentMode(undefined, true, true)).toBe("pro");
  expect(resolveAgentMode("thinking", true, true)).toBe("thinking");
  expect(resolveAgentMode("pro", true, true)).toBe("pro");
  expect(resolveAgentMode("ultra", true, true)).toBe("ultra");
});

test("collapses strength-only modes for a thinking model without effort", () => {
  expect(resolveAgentMode(undefined, true, false)).toBe("thinking");
  expect(resolveAgentMode("pro", true, false)).toBe("thinking");
  expect(resolveAgentMode("ultra", true, false)).toBe("thinking");
});

test("forces flash when the selected model cannot enable thinking", () => {
  expect(resolveAgentMode(undefined, false, true)).toBe("flash");
  expect(resolveAgentMode("ultra", false, true)).toBe("flash");
});

test("maps UI modes to explicit thinking and effort preferences", () => {
  expect(getAgentModeExecutionProfile("flash", true, true)).toEqual({
    thinking_enabled: false,
    reasoning_effort: "none",
  });
  expect(getAgentModeExecutionProfile("thinking", true, true)).toEqual({
    thinking_enabled: true,
    reasoning_effort: "low",
  });
  expect(getAgentModeExecutionProfile("pro", true, true)).toEqual({
    thinking_enabled: true,
    reasoning_effort: "medium",
  });
  expect(getAgentModeExecutionProfile("ultra", true, true)).toEqual({
    thinking_enabled: true,
    reasoning_effort: "high",
  });
});

test("does not request an unsupported reasoning effort", () => {
  expect(getAgentModeExecutionProfile("ultra", true, false)).toEqual({
    thinking_enabled: true,
    reasoning_effort: null,
  });
});

test("sends the capability-aware default mode for a resolved default-bound Agent", () => {
  expect(
    buildRunExecutionProfileRequest({
      mode: "pro",
      modeSelectionExplicit: false,
      modelName: "gpt-5.6-luna",
      modelSelectionExplicit: false,
      agentModelRef: "default",
      model: {
        name: "gpt-5.6-luna",
        supports_thinking: true,
        supports_reasoning_effort: true,
      },
    }),
  ).toEqual({
    model_name: null,
    thinking_enabled: true,
    reasoning_effort: "medium",
  });
});

test("sends the capability-aware default mode for a resolved exact-model Agent", () => {
  expect(
    buildRunExecutionProfileRequest({
      mode: undefined,
      modeSelectionExplicit: false,
      modelName: "gpt-5.6-luna",
      modelSelectionExplicit: false,
      agentModelRef: "gpt-5.6-luna",
      model: {
        name: "gpt-5.6-luna",
        supports_thinking: true,
        supports_reasoning_effort: true,
      },
    }),
  ).toEqual({
    model_name: null,
    thinking_enabled: true,
    reasoning_effort: "medium",
  });
});

test("preserves explicit model and mode choices while the catalog is loading", () => {
  expect(
    buildRunExecutionProfileRequest({
      mode: "ultra",
      modeSelectionExplicit: true,
      modelName: "gpt-5.6-luna",
      modelSelectionExplicit: true,
      model: undefined,
    }),
  ).toEqual({
    model_name: "gpt-5.6-luna",
    thinking_enabled: true,
    reasoning_effort: "high",
  });
});

test("keeps model and mode selections independently authoritative", () => {
  expect(
    buildRunExecutionProfileRequest({
      mode: "flash",
      modeSelectionExplicit: false,
      modelName: "gpt-5.6-luna",
      modelSelectionExplicit: true,
      model: undefined,
    }),
  ).toEqual({
    model_name: "gpt-5.6-luna",
    thinking_enabled: null,
    reasoning_effort: null,
  });
  expect(
    buildRunExecutionProfileRequest({
      mode: "flash",
      modeSelectionExplicit: true,
      modelName: "deepseek-v4-flash",
      modelSelectionExplicit: false,
      model: undefined,
    }),
  ).toEqual({
    model_name: null,
    thinking_enabled: false,
    reasoning_effort: "none",
  });
});

test("omits a conflicting explicit model for an exact-model Agent", () => {
  const models = [
    {
      name: "deepseek-v4-flash",
      supports_thinking: false,
      supports_reasoning_effort: false,
    },
    {
      name: "gpt-5.6-luna",
      supports_thinking: true,
      supports_reasoning_effort: true,
    },
  ];
  const selection = resolveAgentExecutionModelSelection(
    models,
    "gpt-5.6-luna",
    "deepseek-v4-flash",
  );

  expect(selection).toEqual({
    model: models[0],
    modelName: "deepseek-v4-flash",
    modelSelectionLocked: true,
  });
  expect(
    buildRunExecutionProfileRequest({
      mode: "ultra",
      modeSelectionExplicit: true,
      modelName: "gpt-5.6-luna",
      modelSelectionExplicit: true,
      agentModelRef: "deepseek-v4-flash",
      model: selection.model,
    }),
  ).toEqual({
    model_name: null,
    thinking_enabled: false,
    reasoning_effort: null,
  });
});

test("preserves an explicit model for a default-bound Agent", () => {
  const models = [
    {
      name: "deepseek-v4-flash",
      supports_thinking: false,
      supports_reasoning_effort: false,
    },
    {
      name: "gpt-5.6-luna",
      supports_thinking: true,
      supports_reasoning_effort: true,
    },
  ];
  const selection = resolveAgentExecutionModelSelection(
    models,
    "gpt-5.6-luna",
    "default",
  );

  expect(selection).toEqual({
    model: models[1],
    modelName: "gpt-5.6-luna",
    modelSelectionLocked: false,
  });
  expect(
    buildRunExecutionProfileRequest({
      mode: "ultra",
      modeSelectionExplicit: true,
      modelName: "gpt-5.6-luna",
      modelSelectionExplicit: true,
      agentModelRef: "default",
      model: selection.model,
    }),
  ).toEqual({
    model_name: "gpt-5.6-luna",
    thinking_enabled: true,
    reasoning_effort: "high",
  });
});

test("fails safe while the Thread Agent model binding is unresolved", () => {
  expect(
    buildRunExecutionProfileRequest({
      mode: "pro",
      modeSelectionExplicit: true,
      modelName: "gpt-5.6-luna",
      modelSelectionExplicit: true,
      agentModelRef: null,
      model: undefined,
    }),
  ).toEqual({
    model_name: null,
    thinking_enabled: true,
    reasoning_effort: "medium",
  });
});

test("blocks an unresolved or failed Thread Agent model binding", () => {
  expect(
    resolveAgentExecutionAvailability({
      required: true,
      agentModelRef: null,
      agentModelLoading: false,
      agentModelError: null,
      models: [],
      modelsLoading: false,
      modelsError: null,
    }),
  ).toBe("unavailable");
  expect(
    resolveAgentExecutionAvailability({
      required: true,
      agentModelRef: null,
      agentModelLoading: false,
      agentModelError: new Error("version history unavailable"),
      models: [],
      modelsLoading: false,
      modelsError: null,
    }),
  ).toBe("unavailable");
});

test("waits for an exact Agent model and blocks it when absent from the active catalog", () => {
  const base = {
    required: true,
    agentModelRef: "gpt-5.6-luna",
    agentModelLoading: false,
    agentModelError: null,
    models: [] as { name: string }[],
    modelsError: null,
  };

  expect(
    resolveAgentExecutionAvailability({ ...base, modelsLoading: true }),
  ).toBe("loading");
  expect(
    resolveAgentExecutionAvailability({ ...base, modelsLoading: false }),
  ).toBe("unavailable");
  expect(
    resolveAgentExecutionAvailability({
      ...base,
      models: [{ name: "gpt-5.6-luna" }],
      modelsLoading: false,
    }),
  ).toBe("ready");
});

test("keeps a successfully resolved default Agent runnable", () => {
  expect(
    resolveAgentExecutionAvailability({
      required: true,
      agentModelRef: "default",
      agentModelLoading: false,
      agentModelError: null,
      models: [{ name: "gpt-5.6-luna" }],
      modelsLoading: false,
      modelsError: null,
    }),
  ).toBe("ready");
});

test("blocks a default Agent until the active model catalog is usable", () => {
  const base = {
    required: true,
    agentModelRef: "default",
    agentModelLoading: false,
    agentModelError: null,
    models: [] as { name: string }[],
  };

  expect(
    resolveAgentExecutionAvailability({
      ...base,
      modelsLoading: true,
      modelsError: null,
    }),
  ).toBe("loading");
  expect(
    resolveAgentExecutionAvailability({
      ...base,
      modelsLoading: false,
      modelsError: new Error("catalog temporarily unavailable"),
    }),
  ).toBe("unavailable");
  expect(
    resolveAgentExecutionAvailability({
      ...base,
      modelsLoading: false,
      modelsError: null,
    }),
  ).toBe("unavailable");
});

test("does not block a new Thread draft before server Agent metadata exists", () => {
  expect(
    resolveAgentExecutionAvailability({
      required: false,
      agentModelRef: null,
      agentModelLoading: true,
      agentModelError: new Error("metadata does not exist yet"),
      models: [],
      modelsLoading: true,
      modelsError: new Error("catalog unavailable"),
    }),
  ).toBe("ready");
});

test("locks an inherited Sidecar to the parent exact Agent without mutating its preference", () => {
  const sidecarContext = {
    model_name: "gpt-5.6-luna",
    model_selection_explicit: true,
    mode: "ultra" as const,
    mode_selection_explicit: true,
  };
  const selection = resolveAgentExecutionModelSelection(
    [
      {
        name: "deepseek-v4-flash",
        supports_thinking: true,
        supports_reasoning_effort: true,
      },
      {
        name: "gpt-5.6-luna",
        supports_thinking: true,
        supports_reasoning_effort: true,
      },
    ],
    sidecarContext.model_name,
    "deepseek-v4-flash",
  );

  expect(
    buildRunExecutionProfileRequest({
      mode: sidecarContext.mode,
      modeSelectionExplicit: sidecarContext.mode_selection_explicit,
      modelName: sidecarContext.model_name,
      modelSelectionExplicit: sidecarContext.model_selection_explicit,
      agentModelRef: "deepseek-v4-flash",
      model: selection.model,
    }),
  ).toEqual({
    model_name: null,
    thinking_enabled: true,
    reasoning_effort: "high",
  });
  expect(sidecarContext).toEqual({
    model_name: "gpt-5.6-luna",
    model_selection_explicit: true,
    mode: "ultra",
    mode_selection_explicit: true,
  });
});
