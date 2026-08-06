export const SETTINGS_SECTION_IDS = [
  "account",
  "personalization",
  "appearance",
  "notification",
] as const;

export type SettingsSectionId = (typeof SETTINGS_SECTION_IDS)[number];
