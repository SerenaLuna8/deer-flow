import { afterEach, expect, rs, test } from "@rstest/core";

import {
  CHAT_CONTENT_WIDTH_CSS_VALUES,
  DEFAULT_LOCAL_SETTINGS,
  getLocalSettings,
  LOCAL_SETTINGS_KEY,
  normalizeLocalSettings,
  type LocalSettings,
} from "@/core/settings/local";

afterEach(() => {
  rs.unstubAllGlobals();
});

test("defaults token usage to header total plus per-turn breakdown", () => {
  expect(DEFAULT_LOCAL_SETTINGS.tokenUsage).toEqual({
    headerTotal: true,
    inlineMode: "per_turn",
  });
});

test("defaults chat content width to the existing standard layout", () => {
  expect(DEFAULT_LOCAL_SETTINGS.appearance).toEqual({
    chatContentWidth: "standard",
  });
  expect(CHAT_CONTENT_WIDTH_CSS_VALUES).toEqual({
    narrow: "42rem",
    standard: "var(--container-width-md)",
    wide: "var(--container-width-lg)",
    full: "100%",
  });
});

test("normalizes legacy and invalid chat width settings", () => {
  expect(
    normalizeLocalSettings({
      notification: { enabled: false },
    }),
  ).toMatchObject({
    appearance: { chatContentWidth: "standard" },
    notification: { enabled: false },
  });

  expect(
    normalizeLocalSettings({
      appearance: {
        chatContentWidth: "oversized",
      },
    } as unknown as Partial<LocalSettings>),
  ).toMatchObject({
    appearance: { chatContentWidth: "standard" },
  });
});

test("drops legacy standalone reasoning effort from local settings", () => {
  const normalized = normalizeLocalSettings({
    context: {
      mode: "pro",
      reasoning_effort: "high",
    },
  } as unknown as Partial<LocalSettings>);

  expect(normalized.context.mode).toBe("pro");
  expect(normalized.context).not.toHaveProperty("reasoning_effort");
});

test("persists the cleaned local settings after reading a legacy value", () => {
  let stored = JSON.stringify({
    context: {
      mode: "pro",
      reasoning_effort: "high",
    },
  });
  const storage = {
    getItem: rs.fn((key: string) =>
      key === LOCAL_SETTINGS_KEY ? stored : null,
    ),
    setItem: rs.fn((_key: string, value: string) => {
      stored = value;
    }),
  };
  rs.stubGlobal("window", {});
  rs.stubGlobal("localStorage", storage);

  expect(getLocalSettings().context).not.toHaveProperty("reasoning_effort");
  expect(JSON.parse(stored).context).not.toHaveProperty("reasoning_effort");
  expect(storage.setItem).toHaveBeenCalledTimes(1);
});
