export const SETTINGS_SECTION_IDS = [
  "account",
  "appearance",
  "notification",
  "about",
] as const;

export type SettingsSectionId = (typeof SETTINGS_SECTION_IDS)[number];
