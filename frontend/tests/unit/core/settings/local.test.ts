import { afterEach, expect, rs, test } from "@rstest/core";

import {
  applyThreadModeOverride,
  applyThreadModelOverride,
  DEFAULT_LOCAL_SETTINGS,
  getThreadMode,
  getThreadModeSelectionExplicit,
  getThreadModelName,
  getThreadModelSelectionExplicit,
  getLocalSettings,
  LOCAL_SETTINGS_KEY,
  normalizeLocalSettings,
  saveLocalSettings,
  saveThreadMode,
  saveThreadModeSelectionExplicit,
  THREAD_MODE_EXPLICIT_KEY_PREFIX,
  THREAD_MODE_KEY_PREFIX,
  THREAD_MODEL_EXPLICIT_KEY_PREFIX,
  THREAD_MODEL_KEY_PREFIX,
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
  const selected = applyThreadModelOverride(settings, "gpt-5.6-luna", true);

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

  const inherited = applyThreadModeOverride(globalSettings, undefined, false);
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

test("migrates local settings and thread overrides to ActWeave keys", () => {
  const values = new Map<string, string>([
    [
      "deerflow.local-settings",
      JSON.stringify({ appearance: { chatContentWidth: "wide" } }),
    ],
    ["deerflow.thread-model.thread-a", "gpt-5.6-luna"],
    ["deerflow.thread-explicit-model.thread-a", "1"],
    ["deerflow.thread-mode.thread-a", "ultra"],
    ["deerflow.thread-explicit-mode.thread-a", "1"],
  ]);
  const localStorage = {
    getItem: (key: string) => values.get(key) ?? null,
    removeItem: (key: string) => values.delete(key),
    setItem: (key: string, value: string) => values.set(key, value),
  };
  rs.stubGlobal("window", {});
  rs.stubGlobal("localStorage", localStorage);

  expect(LOCAL_SETTINGS_KEY).toBe("actweave.local-settings");
  expect(THREAD_MODEL_KEY_PREFIX).toBe("actweave.thread-model.");
  expect(THREAD_MODEL_EXPLICIT_KEY_PREFIX).toBe(
    "actweave.thread-explicit-model.",
  );
  expect(THREAD_MODE_KEY_PREFIX).toBe("actweave.thread-mode.");
  expect(THREAD_MODE_EXPLICIT_KEY_PREFIX).toBe(
    "actweave.thread-explicit-mode.",
  );

  expect(getLocalSettings().appearance.chatContentWidth).toBe("wide");
  expect(getThreadModelName("thread-a")).toBe("gpt-5.6-luna");
  expect(getThreadModelSelectionExplicit("thread-a")).toBe(true);
  expect(getThreadMode("thread-a")).toBe("ultra");
  expect(getThreadModeSelectionExplicit("thread-a")).toBe(true);
  expect([...values.keys()].some((key) => key.startsWith("deerflow"))).toBe(
    false,
  );
});

test("new local settings remain authoritative over stale legacy data", () => {
  const values = new Map<string, string>([
    [
      "actweave.local-settings",
      JSON.stringify({ appearance: { chatContentWidth: "narrow" } }),
    ],
    [
      "deerflow.local-settings",
      JSON.stringify({ appearance: { chatContentWidth: "wide" } }),
    ],
  ]);
  rs.stubGlobal("window", {});
  rs.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    removeItem: (key: string) => values.delete(key),
    setItem: (key: string, value: string) => values.set(key, value),
  });

  expect(getLocalSettings().appearance.chatContentWidth).toBe("narrow");
  saveLocalSettings(getLocalSettings());
  expect(values.has("deerflow.local-settings")).toBe(false);
});

test("recovers malformed current settings from valid legacy JSON", () => {
  const values = new Map<string, string>([
    ["actweave.local-settings", "{malformed"],
    [
      "deerflow.local-settings",
      JSON.stringify({ appearance: { chatContentWidth: "wide" } }),
    ],
  ]);
  rs.stubGlobal("window", {});
  rs.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    removeItem: (key: string) => values.delete(key),
    setItem: (key: string, value: string) => values.set(key, value),
  });

  expect(getLocalSettings().appearance.chatContentWidth).toBe("wide");
  expect(values.has("deerflow.local-settings")).toBe(false);
  expect(() =>
    JSON.parse(values.get("actweave.local-settings") ?? ""),
  ).not.toThrow();
});

test("does not migrate or delete malformed legacy settings", () => {
  const values = new Map<string, string>([
    ["deerflow.local-settings", "{malformed"],
  ]);
  rs.stubGlobal("window", {});
  rs.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    removeItem: (key: string) => values.delete(key),
    setItem: (key: string, value: string) => values.set(key, value),
  });

  expect(getLocalSettings()).toBe(DEFAULT_LOCAL_SETTINGS);
  expect(Object.fromEntries(values)).toEqual({
    "deerflow.local-settings": "{malformed",
  });
});
