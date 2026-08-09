import type { AgentMode } from "../threads";

import {
  DEFAULT_LOCAL_SETTINGS,
  LOCAL_SETTINGS_KEY,
  THREAD_MODE_EXPLICIT_KEY_PREFIX,
  THREAD_MODE_KEY_PREFIX,
  THREAD_MODEL_EXPLICIT_KEY_PREFIX,
  THREAD_MODEL_KEY_PREFIX,
  getLocalSettings,
  getThreadMode,
  getThreadModeSelectionExplicit,
  getThreadModelName,
  getThreadModelSelectionExplicit,
  saveLocalSettings,
  saveThreadMode,
  saveThreadModeSelectionExplicit,
  saveThreadModelName,
  saveThreadModelSelectionExplicit,
  type LocalSettings,
} from "./local";

type Listener = () => void;

export type LocalSettingsSetter = <K extends keyof LocalSettings>(
  key: K,
  value: Partial<LocalSettings[K]>,
) => void;

const listeners = new Set<Listener>();
const threadModelNames = new Map<string, string | undefined>();
const threadModelSelections = new Map<string, boolean>();
const threadModes = new Map<string, AgentMode | undefined>();
const threadModeSelections = new Map<string, boolean>();

let baseSettings: LocalSettings = DEFAULT_LOCAL_SETTINGS;
let baseSettingsLoaded = false;
let storageListenerRegistered = false;

function emitChange() {
  for (const listener of listeners) {
    listener();
  }
}

function ensureBaseSettingsLoaded() {
  if (baseSettingsLoaded || typeof window === "undefined") {
    return;
  }

  baseSettings = getLocalSettings();
  baseSettingsLoaded = true;
}

function ensureStorageListenerRegistered() {
  if (storageListenerRegistered || typeof window === "undefined") {
    return;
  }

  window.addEventListener("storage", handleStorage);
  storageListenerRegistered = true;
}

function mergeSettingsSection<K extends keyof LocalSettings>(
  settings: LocalSettings,
  key: K,
  value: Partial<LocalSettings[K]>,
): LocalSettings {
  return {
    ...settings,
    [key]: {
      ...settings[key],
      ...value,
    },
  } as LocalSettings;
}

function handleStorage(event: StorageEvent) {
  if (event.storageArea && event.storageArea !== localStorage) {
    return;
  }

  ensureBaseSettingsLoaded();

  if (event.key === null) {
    baseSettings = getLocalSettings();
    threadModelNames.clear();
    threadModelSelections.clear();
    threadModes.clear();
    threadModeSelections.clear();
    emitChange();
    return;
  }

  if (event.key === LOCAL_SETTINGS_KEY) {
    baseSettings = getLocalSettings();
    emitChange();
    return;
  }

  if (event.key.startsWith(THREAD_MODEL_EXPLICIT_KEY_PREFIX)) {
    const threadId = event.key.slice(THREAD_MODEL_EXPLICIT_KEY_PREFIX.length);
    threadModelSelections.set(
      threadId,
      getThreadModelSelectionExplicit(threadId),
    );
    emitChange();
    return;
  }

  if (event.key.startsWith(THREAD_MODE_EXPLICIT_KEY_PREFIX)) {
    const threadId = event.key.slice(THREAD_MODE_EXPLICIT_KEY_PREFIX.length);
    threadModeSelections.set(
      threadId,
      getThreadModeSelectionExplicit(threadId),
    );
    emitChange();
    return;
  }

  if (event.key.startsWith(THREAD_MODE_KEY_PREFIX)) {
    const threadId = event.key.slice(THREAD_MODE_KEY_PREFIX.length);
    threadModes.set(threadId, getThreadMode(threadId));
    emitChange();
    return;
  }

  if (!event.key.startsWith(THREAD_MODEL_KEY_PREFIX)) return;
  const threadId = event.key.slice(THREAD_MODEL_KEY_PREFIX.length);
  threadModelNames.set(threadId, getThreadModelName(threadId));
  emitChange();
}

export function subscribe(listener: Listener): () => void {
  ensureBaseSettingsLoaded();
  ensureStorageListenerRegistered();
  listeners.add(listener);

  return () => {
    listeners.delete(listener);
  };
}

export function getBaseSettingsSnapshot(): LocalSettings {
  ensureBaseSettingsLoaded();
  return baseSettings;
}

export function getThreadModelSnapshot(threadId: string): string | undefined {
  ensureBaseSettingsLoaded();

  if (!threadModelNames.has(threadId)) {
    threadModelNames.set(threadId, getThreadModelName(threadId));
  }

  return threadModelNames.get(threadId);
}

export function getThreadModelSelectionSnapshot(threadId: string): boolean {
  ensureBaseSettingsLoaded();

  if (!threadModelSelections.has(threadId)) {
    threadModelSelections.set(
      threadId,
      getThreadModelSelectionExplicit(threadId),
    );
  }

  return threadModelSelections.get(threadId) ?? false;
}

export function getThreadModeSnapshot(threadId: string): AgentMode | undefined {
  ensureBaseSettingsLoaded();

  if (!threadModes.has(threadId)) {
    threadModes.set(threadId, getThreadMode(threadId));
  }

  return threadModes.get(threadId);
}

export function getThreadModeSelectionSnapshot(threadId: string): boolean {
  ensureBaseSettingsLoaded();

  if (!threadModeSelections.has(threadId)) {
    threadModeSelections.set(
      threadId,
      getThreadModeSelectionExplicit(threadId),
    );
  }

  return threadModeSelections.get(threadId) ?? false;
}

export const updateLocalSettings: LocalSettingsSetter = (key, value) => {
  ensureBaseSettingsLoaded();
  ensureStorageListenerRegistered();

  baseSettings = mergeSettingsSection(baseSettings, key, value);
  saveLocalSettings(baseSettings);
  emitChange();
};

export function updateThreadSettings<K extends keyof LocalSettings>(
  threadId: string,
  key: K,
  value: Partial<LocalSettings[K]>,
) {
  ensureBaseSettingsLoaded();
  ensureStorageListenerRegistered();

  const nextBaseSettings = mergeSettingsSection(baseSettings, key, value);
  baseSettings = nextBaseSettings;
  saveLocalSettings(baseSettings);

  if (key === "context") {
    const contextValue = value as Partial<LocalSettings["context"]>;
    const hasModel = Object.prototype.hasOwnProperty.call(
      contextValue,
      "model_name",
    );
    const hasModelExplicit = Object.prototype.hasOwnProperty.call(
      contextValue,
      "model_selection_explicit",
    );
    const modelExplicit = hasModelExplicit
      ? contextValue.model_selection_explicit === true
      : (threadModelSelections.get(threadId) ??
        getThreadModelSelectionExplicit(threadId));

    if (hasModelExplicit) {
      threadModelSelections.set(threadId, modelExplicit);
      saveThreadModelSelectionExplicit(threadId, modelExplicit);
    }
    if (hasModel || (hasModelExplicit && !modelExplicit)) {
      const threadModelName = modelExplicit
        ? contextValue.model_name
        : undefined;
      threadModelNames.set(threadId, threadModelName);
      saveThreadModelName(threadId, threadModelName);
    }

    const hasMode = Object.prototype.hasOwnProperty.call(contextValue, "mode");
    const hasModeExplicit = Object.prototype.hasOwnProperty.call(
      contextValue,
      "mode_selection_explicit",
    );
    const modeExplicit = hasModeExplicit
      ? contextValue.mode_selection_explicit === true
      : (threadModeSelections.get(threadId) ??
        getThreadModeSelectionExplicit(threadId));

    if (hasModeExplicit) {
      threadModeSelections.set(threadId, modeExplicit);
      saveThreadModeSelectionExplicit(threadId, modeExplicit);
    }
    if (hasMode || (hasModeExplicit && !modeExplicit)) {
      const threadMode = modeExplicit ? contextValue.mode : undefined;
      threadModes.set(threadId, threadMode);
      saveThreadMode(threadId, threadMode);
    }
  }

  emitChange();
}
