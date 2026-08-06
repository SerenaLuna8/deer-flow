import { useCallback, useMemo, useSyncExternalStore } from "react";

import {
  DEFAULT_LOCAL_SETTINGS,
  applyThreadModeOverride,
  applyThreadModelOverride,
  type LocalSettings,
} from "./local";
import {
  getBaseSettingsSnapshot,
  getThreadModeSelectionSnapshot,
  getThreadModeSnapshot,
  getThreadModelSnapshot,
  getThreadModelSelectionSnapshot,
  subscribe,
  updateLocalSettings,
  updateThreadSettings,
  type LocalSettingsSetter,
} from "./store";

export function useLocalSettings(): [LocalSettings, LocalSettingsSetter] {
  const settings = useSyncExternalStore(
    subscribe,
    getBaseSettingsSnapshot,
    () => DEFAULT_LOCAL_SETTINGS,
  );

  const setSettings = useCallback<LocalSettingsSetter>((key, value) => {
    updateLocalSettings(key, value);
  }, []);

  return [settings, setSettings];
}

export function useThreadSettings(
  threadId: string,
): [LocalSettings, LocalSettingsSetter] {
  const baseSettings = useSyncExternalStore(
    subscribe,
    getBaseSettingsSnapshot,
    () => DEFAULT_LOCAL_SETTINGS,
  );

  const threadModelName = useSyncExternalStore(
    subscribe,
    () => getThreadModelSnapshot(threadId),
    () => undefined,
  );
  const threadModelSelectionExplicit = useSyncExternalStore(
    subscribe,
    () => getThreadModelSelectionSnapshot(threadId),
    () => false,
  );
  const threadMode = useSyncExternalStore(
    subscribe,
    () => getThreadModeSnapshot(threadId),
    () => undefined,
  );
  const threadModeSelectionExplicit = useSyncExternalStore(
    subscribe,
    () => getThreadModeSelectionSnapshot(threadId),
    () => false,
  );

  const settings = useMemo(
    () => {
      const modelSettings = applyThreadModelOverride(
        baseSettings,
        threadModelName,
        threadModelSelectionExplicit,
      );
      return applyThreadModeOverride(
        modelSettings,
        threadMode,
        threadModeSelectionExplicit,
      );
    },
    [
      baseSettings,
      threadMode,
      threadModeSelectionExplicit,
      threadModelName,
      threadModelSelectionExplicit,
    ],
  );

  const setSettings = useCallback<LocalSettingsSetter>(
    (key, value) => {
      updateThreadSettings(threadId, key, value);
    },
    [threadId],
  );

  return [settings, setSettings];
}
