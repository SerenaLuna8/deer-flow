import { describe, expect, test, rs } from "@rstest/core";

import {
  readMigratedStorageValue,
  removeMigratedStorageValue,
  writeMigratedStorageValue,
  type BrandStorage,
} from "@/core/storage/brand-key-migration";

function memoryStorage(initial: Record<string, string> = {}): {
  storage: BrandStorage;
  values: Map<string, string>;
} {
  const values = new Map(Object.entries(initial));
  return {
    storage: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key),
    },
    values,
  };
}

describe("ActWeave browser storage key migration", () => {
  test("prefers the new key and cleans a redundant legacy key", () => {
    const { storage, values } = memoryStorage({
      "actweave.preference": "new",
      "deerflow.preference": "old",
    });

    expect(
      readMigratedStorageValue(storage, "actweave.preference", [
        "deerflow.preference",
      ]),
    ).toBe("new");
    expect(values.has("deerflow.preference")).toBe(false);
  });

  test("copies a legacy value before deleting its old key", () => {
    const operations: string[] = [];
    const { storage, values } = memoryStorage({
      "deerflow.preference": "legacy",
    });
    const tracked: BrandStorage = {
      getItem: storage.getItem,
      setItem(key, value) {
        operations.push(`set:${key}`);
        storage.setItem(key, value);
      },
      removeItem(key) {
        operations.push(`remove:${key}`);
        storage.removeItem(key);
      },
    };

    expect(
      readMigratedStorageValue(tracked, "actweave.preference", [
        "deerflow.preference",
      ]),
    ).toBe("legacy");
    expect(values.get("actweave.preference")).toBe("legacy");
    expect(values.has("deerflow.preference")).toBe(false);
    expect(operations).toEqual([
      "set:actweave.preference",
      "remove:deerflow.preference",
    ]);
  });

  test("keeps the legacy value when the new write fails", () => {
    const removeItem = rs.fn();
    const storage: BrandStorage = {
      getItem: (key) => (key === "deerflow.preference" ? "legacy" : null),
      setItem: () => {
        throw new Error("quota denied");
      },
      removeItem,
    };

    expect(
      readMigratedStorageValue(storage, "actweave.preference", [
        "deerflow.preference",
      ]),
    ).toBe("legacy");
    expect(removeItem).not.toHaveBeenCalled();
  });

  test("replaces a malformed current value from a valid legacy value", () => {
    const { storage, values } = memoryStorage({
      "actweave.preference": "malformed",
      "deerflow.preference": "valid:legacy",
    });

    expect(
      readMigratedStorageValue(
        storage,
        "actweave.preference",
        ["deerflow.preference"],
        (value) => value.startsWith("valid:"),
      ),
    ).toBe("valid:legacy");
    expect(Object.fromEntries(values)).toEqual({
      "actweave.preference": "valid:legacy",
    });
  });

  test("leaves malformed legacy data untouched when no valid value exists", () => {
    const { storage, values } = memoryStorage({
      "deerflow.preference": "malformed",
    });

    expect(
      readMigratedStorageValue(
        storage,
        "actweave.preference",
        ["deerflow.preference"],
        (value) => value.startsWith("valid:"),
      ),
    ).toBeNull();
    expect(Object.fromEntries(values)).toEqual({
      "deerflow.preference": "malformed",
    });
  });

  test("writes and removes the complete key family without storing extra data", () => {
    const { storage, values } = memoryStorage({
      "deerflow.preference": "legacy",
    });

    expect(
      writeMigratedStorageValue(storage, "actweave.preference", "safe-value", [
        "deerflow.preference",
      ]),
    ).toBe(true);
    expect(Object.fromEntries(values)).toEqual({
      "actweave.preference": "safe-value",
    });

    removeMigratedStorageValue(storage, "actweave.preference", [
      "deerflow.preference",
    ]);
    expect(values.size).toBe(0);
  });
});
