export const SETTINGS_SECTION_IDS = [
  "account",
  "personalization",
  "appearance",
] as const;

export type SettingsSectionId = (typeof SETTINGS_SECTION_IDS)[number];
