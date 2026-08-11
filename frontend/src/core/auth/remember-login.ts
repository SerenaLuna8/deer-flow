import {
  readMigratedStorageValue,
  removeMigratedStorageValue,
  writeMigratedStorageValue,
  type BrandStorage,
} from "@/core/storage/brand-key-migration";

const REMEMBER_LOGIN_KEY = "actweave.auth.remember_login";
const REMEMBERED_EMAIL_KEY = "actweave.auth.remembered_email";
const LEGACY_REMEMBER_LOGIN_KEY = "deerflow.auth.remember_login";
const LEGACY_REMEMBERED_EMAIL_KEY = "deerflow.auth.remembered_email";

export interface RememberLoginPreference {
  email: string;
  rememberMe: boolean;
}

const DEFAULT_PREFERENCE: RememberLoginPreference = {
  email: "",
  rememberMe: true,
};

function isStoredRememberLoginPreference(value: string): boolean {
  return value === "0" || value === "1";
}

function getStorage(): BrandStorage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function loadRememberLoginPreference(): RememberLoginPreference {
  const storage = getStorage();
  if (!storage) return DEFAULT_PREFERENCE;

  const storedPreference = readMigratedStorageValue(
    storage,
    REMEMBER_LOGIN_KEY,
    [LEGACY_REMEMBER_LOGIN_KEY],
    isStoredRememberLoginPreference,
  );
  const rememberMe =
    storedPreference === null ? true : storedPreference === "1";
  if (!rememberMe) {
    removeMigratedStorageValue(storage, REMEMBERED_EMAIL_KEY, [
      LEGACY_REMEMBERED_EMAIL_KEY,
    ]);
  }
  return {
    email: rememberMe
      ? (readMigratedStorageValue(storage, REMEMBERED_EMAIL_KEY, [
          LEGACY_REMEMBERED_EMAIL_KEY,
        ]) ?? "")
      : "",
    rememberMe,
  };
}

/**
 * Persist only the user's convenience preference and email address.
 * Passwords and session material never enter Web Storage.
 */
export function saveRememberLoginPreference({
  email,
  rememberMe,
}: RememberLoginPreference): void {
  const storage = getStorage();
  if (!storage) return;

  writeMigratedStorageValue(
    storage,
    REMEMBER_LOGIN_KEY,
    rememberMe ? "1" : "0",
    [LEGACY_REMEMBER_LOGIN_KEY],
  );
  if (rememberMe) {
    writeMigratedStorageValue(storage, REMEMBERED_EMAIL_KEY, email, [
      LEGACY_REMEMBERED_EMAIL_KEY,
    ]);
  } else {
    removeMigratedStorageValue(storage, REMEMBERED_EMAIL_KEY, [
      LEGACY_REMEMBERED_EMAIL_KEY,
    ]);
  }
}
