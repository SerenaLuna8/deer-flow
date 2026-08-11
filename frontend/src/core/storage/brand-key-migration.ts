export type BrandStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function removeKeys(storage: BrandStorage, keys: readonly string[]): void {
  for (const key of keys) {
    try {
      storage.removeItem(key);
    } catch {
      // Storage cleanup is best-effort in locked-down browser contexts.
    }
  }
}

export function readMigratedStorageValue(
  storage: BrandStorage,
  currentKey: string,
  legacyKeys: readonly string[],
  isValid: (value: string) => boolean = () => true,
): string | null {
  try {
    const currentValue = storage.getItem(currentKey);
    if (currentValue !== null && isValid(currentValue)) {
      removeKeys(storage, legacyKeys);
      return currentValue;
    }
  } catch {
    // A legacy read may still work with a storage adapter that failed one key.
  }

  for (const legacyKey of legacyKeys) {
    let legacyValue: string | null;
    try {
      legacyValue = storage.getItem(legacyKey);
    } catch {
      continue;
    }
    if (legacyValue === null || !isValid(legacyValue)) {
      continue;
    }

    try {
      storage.setItem(currentKey, legacyValue);
    } catch {
      return legacyValue;
    }
    removeKeys(storage, legacyKeys);
    return legacyValue;
  }

  return null;
}

export function writeMigratedStorageValue(
  storage: BrandStorage,
  currentKey: string,
  value: string,
  legacyKeys: readonly string[],
): boolean {
  try {
    storage.setItem(currentKey, value);
  } catch {
    return false;
  }
  removeKeys(storage, legacyKeys);
  return true;
}

export function removeMigratedStorageValue(
  storage: BrandStorage,
  currentKey: string,
  legacyKeys: readonly string[],
): void {
  removeKeys(storage, [currentKey, ...legacyKeys]);
}
