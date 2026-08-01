import type { TokenUsageInlineMode } from "../messages/usage-model";
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
    mode: undefined,
  },
};

export const LOCAL_SETTINGS_KEY = "deerflow.local-settings";
export const THREAD_MODEL_KEY_PREFIX = "deerflow.thread-model.";

function isBrowser(): boolean {
  return typeof window !== "undefined";
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
    mode: AgentMode | undefined;
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

export function getThreadModelName(threadId: string): string | undefined {
  if (!isBrowser()) {
    return undefined;
  }
  return localStorage.getItem(getThreadModelStorageKey(threadId)) ?? undefined;
}

export function saveThreadModelName(
  threadId: string,
  modelName: string | undefined,
) {
  if (!isBrowser()) {
    return;
  }
  const key = getThreadModelStorageKey(threadId);
  if (!modelName) {
    localStorage.removeItem(key);
    return;
  }
  localStorage.setItem(key, modelName);
}

export function applyThreadModelOverride(
  settings: LocalSettings,
  threadModelName: string | undefined,
): LocalSettings {
  if (!threadModelName) {
    return settings;
  }
  return {
    ...settings,
    context: {
      ...settings.context,
      model_name: threadModelName,
    },
  };
}

export function getLocalSettings(): LocalSettings {
  if (!isBrowser()) {
    return DEFAULT_LOCAL_SETTINGS;
  }
  const json = localStorage.getItem(LOCAL_SETTINGS_KEY);
  try {
    if (json) {
      const settings = JSON.parse(json) as Partial<LocalSettings>;
      const normalized = normalizeLocalSettings(settings);
      const normalizedJson = JSON.stringify(normalized);
      if (json !== normalizedJson) {
        localStorage.setItem(LOCAL_SETTINGS_KEY, normalizedJson);
      }
      return normalized;
    }
  } catch {}
  return DEFAULT_LOCAL_SETTINGS;
}

export function saveLocalSettings(settings: LocalSettings) {
  if (!isBrowser()) {
    return;
  }
  localStorage.setItem(
    LOCAL_SETTINGS_KEY,
    JSON.stringify(normalizeLocalSettings(settings)),
  );
}
