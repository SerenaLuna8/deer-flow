import { expect, test } from "@rstest/core";

import {
  CHAT_CONTENT_WIDTH_CSS_VALUES,
  DEFAULT_LOCAL_SETTINGS,
  normalizeLocalSettings,
  type LocalSettings,
} from "@/core/settings/local";

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
