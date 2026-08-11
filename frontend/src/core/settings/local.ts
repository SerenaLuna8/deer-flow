import type { TokenUsageInlineMode } from "../messages/usage-model";
import {
  readMigratedStorageValue,
  removeMigratedStorageValue,
  writeMigratedStorageValue,
  type BrandStorage,
} from "../storage/brand-key-migration";
import {
  isAgentMode,
  type AgentMode,
  type AgentThreadContext,
} from "../threads";

export const CHAT_CONTENT_WIDTH_OPTIONS = [
  "narrow",
  "standard",
  "wide",
  "full",
] as const;

export type ChatContentWidth = (typeof CHAT_CONTENT_WIDTH_OPTIONS)[number];

export const CHAT_CONTENT_WIDTH_CSS_VALUES: Record<ChatContentWidth, string> = {
  narrow: "42rem",
  standard: "var(--container-width-md)",
  wide: "var(--container-width-lg)",
  full: "100%",
};

export const DEFAULT_LOCAL_SETTINGS: LocalSettings = {
  appearance: {
    chatContentWidth: "standard",
  },
  notification: {
    enabled: true,
  },
  tokenUsage: {
    headerTotal: true,
    inlineMode: "per_turn",
  },
  context: {
    model_name: undefined,
    model_selection_explicit: false,
    mode: undefined,
    mode_selection_explicit: false,
  },
};

export const LOCAL_SETTINGS_KEY = "actweave.local-settings";
export const THREAD_MODEL_KEY_PREFIX = "actweave.thread-model.";
export const THREAD_MODEL_EXPLICIT_KEY_PREFIX =
  "actweave.thread-explicit-model.";
export const THREAD_MODE_KEY_PREFIX = "actweave.thread-mode.";
export const THREAD_MODE_EXPLICIT_KEY_PREFIX = "actweave.thread-explicit-mode.";

export const LEGACY_LOCAL_SETTINGS_KEY = "deerflow.local-settings";
export const LEGACY_THREAD_MODEL_KEY_PREFIX = "deerflow.thread-model.";
export const LEGACY_THREAD_MODEL_EXPLICIT_KEY_PREFIX =
  "deerflow.thread-explicit-model.";
export const LEGACY_THREAD_MODE_KEY_PREFIX = "deerflow.thread-mode.";
export const LEGACY_THREAD_MODE_EXPLICIT_KEY_PREFIX =
  "deerflow.thread-explicit-mode.";

function isBrowser(): boolean {
  return typeof window !== "undefined";
}

function getBrowserStorage(): BrandStorage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

function isStoredLocalSettings(value: string): boolean {
  try {
    const parsed: unknown = JSON.parse(value);
    return (
      typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
    );
  } catch {
    return false;
  }
}

function isStoredModelName(value: string): boolean {
  return value.length > 0;
}

function isStoredExplicitSelection(value: string): boolean {
  return value === "1";
}

export interface LocalSettings {
  appearance: {
    chatContentWidth: ChatContentWidth;
  };
  notification: {
    enabled: boolean;
  };
  tokenUsage: {
    headerTotal: boolean;
    inlineMode: TokenUsageInlineMode;
  };
  context: Omit<
    AgentThreadContext,
    | "thread_id"
    | "is_plan_mode"
    | "thinking_enabled"
    | "subagent_enabled"
    | "model_name"
    | "reasoning_effort"
  > & {
    model_name?: string | undefined;
    model_selection_explicit?: boolean;
    mode: AgentMode | undefined;
    mode_selection_explicit?: boolean;
  };
}

export function isChatContentWidth(value: unknown): value is ChatContentWidth {
  return CHAT_CONTENT_WIDTH_OPTIONS.some((option) => option === value);
}

export function normalizeLocalSettings(
  settings?: Partial<LocalSettings>,
): LocalSettings {
  const storedChatContentWidth = settings?.appearance?.chatContentWidth;
  const normalizedContext = {
    ...DEFAULT_LOCAL_SETTINGS.context,
    ...settings?.context,
  };
  Reflect.deleteProperty(normalizedContext, "reasoning_effort");
  normalizedContext.mode = isAgentMode(normalizedContext.mode)
    ? normalizedContext.mode
    : undefined;
  normalizedContext.model_selection_explicit =
    normalizedContext.model_selection_explicit === true;
  normalizedContext.mode_selection_explicit =
    normalizedContext.mode_selection_explicit === true;
  return {
    ...DEFAULT_LOCAL_SETTINGS,
    ...settings,
    appearance: {
      ...DEFAULT_LOCAL_SETTINGS.appearance,
      ...settings?.appearance,
      chatContentWidth: isChatContentWidth(storedChatContentWidth)
        ? storedChatContentWidth
        : DEFAULT_LOCAL_SETTINGS.appearance.chatContentWidth,
    },
    context: normalizedContext,
    tokenUsage: {
      ...DEFAULT_LOCAL_SETTINGS.tokenUsage,
      ...settings?.tokenUsage,
    },
    notification: {
      ...DEFAULT_LOCAL_SETTINGS.notification,
      ...settings?.notification,
    },
  };
}

function getThreadModelStorageKey(threadId: string): string {
  return `${THREAD_MODEL_KEY_PREFIX}${threadId}`;
}

function getLegacyThreadModelStorageKey(threadId: string): string {
  return `${LEGACY_THREAD_MODEL_KEY_PREFIX}${threadId}`;
}

export function getThreadModelName(threadId: string): string | undefined {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return undefined;
  }
  return (
    readMigratedStorageValue(
      storage,
      getThreadModelStorageKey(threadId),
      [getLegacyThreadModelStorageKey(threadId)],
      isStoredModelName,
    ) ?? undefined
  );
}

export function saveThreadModelName(
  threadId: string,
  modelName: string | undefined,
) {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return;
  }
  const key = getThreadModelStorageKey(threadId);
  const legacyKeys = [getLegacyThreadModelStorageKey(threadId)];
  if (!modelName) {
    removeMigratedStorageValue(storage, key, legacyKeys);
    return;
  }
  writeMigratedStorageValue(storage, key, modelName, legacyKeys);
}

function getThreadModelExplicitStorageKey(threadId: string): string {
  return `${THREAD_MODEL_EXPLICIT_KEY_PREFIX}${threadId}`;
}

function getLegacyThreadModelExplicitStorageKey(threadId: string): string {
  return `${LEGACY_THREAD_MODEL_EXPLICIT_KEY_PREFIX}${threadId}`;
}

export function getThreadModelSelectionExplicit(threadId: string): boolean {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return false;
  }
  return (
    readMigratedStorageValue(
      storage,
      getThreadModelExplicitStorageKey(threadId),
      [getLegacyThreadModelExplicitStorageKey(threadId)],
      isStoredExplicitSelection,
    ) === "1"
  );
}

export function saveThreadModelSelectionExplicit(
  threadId: string,
  explicit: boolean,
) {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return;
  }
  const key = getThreadModelExplicitStorageKey(threadId);
  const legacyKeys = [getLegacyThreadModelExplicitStorageKey(threadId)];
  if (!explicit) {
    removeMigratedStorageValue(storage, key, legacyKeys);
    return;
  }
  writeMigratedStorageValue(storage, key, "1", legacyKeys);
}

export function applyThreadModelOverride(
  settings: LocalSettings,
  threadModelName: string | undefined,
  threadModelSelectionExplicit = false,
): LocalSettings {
  if (!threadModelName || !threadModelSelectionExplicit) {
    return settings;
  }
  return {
    ...settings,
    context: {
      ...settings.context,
      model_name: threadModelName,
      model_selection_explicit: threadModelSelectionExplicit,
    },
  };
}

function getThreadModeStorageKey(threadId: string): string {
  return `${THREAD_MODE_KEY_PREFIX}${threadId}`;
}

function getLegacyThreadModeStorageKey(threadId: string): string {
  return `${LEGACY_THREAD_MODE_KEY_PREFIX}${threadId}`;
}

export function getThreadMode(threadId: string): AgentMode | undefined {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return undefined;
  }
  const mode = readMigratedStorageValue(
    storage,
    getThreadModeStorageKey(threadId),
    [getLegacyThreadModeStorageKey(threadId)],
    isAgentMode,
  );
  return isAgentMode(mode) ? mode : undefined;
}

export function saveThreadMode(threadId: string, mode: AgentMode | undefined) {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return;
  }
  const key = getThreadModeStorageKey(threadId);
  const legacyKeys = [getLegacyThreadModeStorageKey(threadId)];
  if (!mode) {
    removeMigratedStorageValue(storage, key, legacyKeys);
    return;
  }
  writeMigratedStorageValue(storage, key, mode, legacyKeys);
}

function getThreadModeExplicitStorageKey(threadId: string): string {
  return `${THREAD_MODE_EXPLICIT_KEY_PREFIX}${threadId}`;
}

function getLegacyThreadModeExplicitStorageKey(threadId: string): string {
  return `${LEGACY_THREAD_MODE_EXPLICIT_KEY_PREFIX}${threadId}`;
}

export function getThreadModeSelectionExplicit(threadId: string): boolean {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return false;
  }
  return (
    readMigratedStorageValue(
      storage,
      getThreadModeExplicitStorageKey(threadId),
      [getLegacyThreadModeExplicitStorageKey(threadId)],
      isStoredExplicitSelection,
    ) === "1"
  );
}

export function saveThreadModeSelectionExplicit(
  threadId: string,
  explicit: boolean,
) {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return;
  }
  const key = getThreadModeExplicitStorageKey(threadId);
  const legacyKeys = [getLegacyThreadModeExplicitStorageKey(threadId)];
  if (!explicit) {
    removeMigratedStorageValue(storage, key, legacyKeys);
    return;
  }
  writeMigratedStorageValue(storage, key, "1", legacyKeys);
}

export function applyThreadModeOverride(
  settings: LocalSettings,
  threadMode: AgentMode | undefined,
  threadModeSelectionExplicit = false,
): LocalSettings {
  if (!threadMode || !threadModeSelectionExplicit) {
    return settings;
  }
  return {
    ...settings,
    context: {
      ...settings.context,
      mode: threadMode,
      mode_selection_explicit: true,
    },
  };
}

export function getLocalSettings(): LocalSettings {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return DEFAULT_LOCAL_SETTINGS;
  }
  const json = readMigratedStorageValue(
    storage,
    LOCAL_SETTINGS_KEY,
    [LEGACY_LOCAL_SETTINGS_KEY],
    isStoredLocalSettings,
  );
  try {
    if (json) {
      const settings = JSON.parse(json) as Partial<LocalSettings>;
      const normalized = normalizeLocalSettings(settings);
      const normalizedJson = JSON.stringify(normalized);
      if (json !== normalizedJson) {
        writeMigratedStorageValue(storage, LOCAL_SETTINGS_KEY, normalizedJson, [
          LEGACY_LOCAL_SETTINGS_KEY,
        ]);
      }
      return normalized;
    }
  } catch {}
  return DEFAULT_LOCAL_SETTINGS;
}

export function saveLocalSettings(settings: LocalSettings) {
  const storage = isBrowser() ? getBrowserStorage() : null;
  if (!storage) {
    return;
  }
  writeMigratedStorageValue(
    storage,
    LOCAL_SETTINGS_KEY,
    JSON.stringify(normalizeLocalSettings(settings)),
    [LEGACY_LOCAL_SETTINGS_KEY],
  );
}
