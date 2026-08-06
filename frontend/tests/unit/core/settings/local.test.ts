import { afterEach, expect, rs, test } from "@rstest/core";

import {
  applyThreadModeOverride,
  applyThreadModelOverride,
  getThreadMode,
  getThreadModeSelectionExplicit,
  normalizeLocalSettings,
  saveThreadMode,
  saveThreadModeSelectionExplicit,
} from "@/core/settings/local";

afterEach(() => {
  rs.unstubAllGlobals();
});

test("migrates legacy automatic selections as non-authoritative", () => {
  const settings = normalizeLocalSettings({
    context: {
      model_name: "deepseek-v4-flash",
      mode: "pro",
    },
  });

  expect(settings.context.model_selection_explicit).toBe(false);
  expect(settings.context.mode_selection_explicit).toBe(false);
});

test("applies a per-thread explicit model selection independently", () => {
  const settings = normalizeLocalSettings();
  const selected = applyThreadModelOverride(
    settings,
    "gpt-5.6-luna",
    true,
  );

  expect(selected.context.model_name).toBe("gpt-5.6-luna");
  expect(selected.context.model_selection_explicit).toBe(true);
});

test("applies an explicit mode per thread and otherwise keeps the global fallback", () => {
  const globalSettings = normalizeLocalSettings({
    context: {
      mode: "pro",
      mode_selection_explicit: true,
    },
  });

  const inherited = applyThreadModeOverride(
    globalSettings,
    undefined,
    false,
  );
  const selected = applyThreadModeOverride(globalSettings, "ultra", true);

  expect(inherited.context.mode).toBe("pro");
  expect(inherited.context.mode_selection_explicit).toBe(true);
  expect(selected.context.mode).toBe("ultra");
  expect(selected.context.mode_selection_explicit).toBe(true);
});

test("persists mode and its explicit marker independently for each thread", () => {
  const values = new Map<string, string>();
  const localStorage = {
    getItem: (key: string) => values.get(key) ?? null,
    removeItem: (key: string) => values.delete(key),
    setItem: (key: string, value: string) => values.set(key, value),
  };
  rs.stubGlobal("window", {});
  rs.stubGlobal("localStorage", localStorage);

  saveThreadMode("thread-a", "ultra");
  saveThreadModeSelectionExplicit("thread-a", true);
  saveThreadMode("thread-b", "flash");
  saveThreadModeSelectionExplicit("thread-b", true);

  expect(getThreadMode("thread-a")).toBe("ultra");
  expect(getThreadModeSelectionExplicit("thread-a")).toBe(true);
  expect(getThreadMode("thread-b")).toBe("flash");
  expect(getThreadModeSelectionExplicit("thread-b")).toBe(true);
});
