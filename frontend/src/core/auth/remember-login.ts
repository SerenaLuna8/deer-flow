const REMEMBER_LOGIN_KEY = "deerflow.auth.remember_login";
const REMEMBERED_EMAIL_KEY = "deerflow.auth.remembered_email";

export interface RememberLoginPreference {
  email: string;
  rememberMe: boolean;
}

const DEFAULT_PREFERENCE: RememberLoginPreference = {
  email: "",
  rememberMe: true,
};

function getStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function loadRememberLoginPreference(): RememberLoginPreference {
  const storage = getStorage();
  if (!storage) return DEFAULT_PREFERENCE;

  try {
    const storedPreference = storage.getItem(REMEMBER_LOGIN_KEY);
    const rememberMe =
      storedPreference === null ? true : storedPreference === "1";
    return {
      email: rememberMe ? (storage.getItem(REMEMBERED_EMAIL_KEY) ?? "") : "",
      rememberMe,
    };
  } catch {
    return DEFAULT_PREFERENCE;
  }
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

  try {
    if (rememberMe) {
      storage.setItem(REMEMBER_LOGIN_KEY, "1");
      storage.setItem(REMEMBERED_EMAIL_KEY, email);
      return;
    }
    storage.setItem(REMEMBER_LOGIN_KEY, "0");
    storage.removeItem(REMEMBERED_EMAIL_KEY);
  } catch {
    // Storage can be denied in private/locked-down browser contexts.
  }
}
