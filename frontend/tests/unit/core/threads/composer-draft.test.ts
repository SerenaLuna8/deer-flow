import { describe, expect, test } from "@rstest/core";

import {
  buildComposerDraftKey,
  clearComposerDraft,
  readComposerDraft,
  writeComposerDraft,
  type ComposerDraftStorage,
} from "@/core/threads/composer-draft";

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  const storage: ComposerDraftStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  return { storage, values };
}

describe("composer draft brand key migration", () => {
  test("builds an ActWeave key and migrates a valid legacy draft", () => {
    const key = buildComposerDraftKey({
      accountId: "account-1",
      projectId: "project-1",
      agentName: "main",
      conversationScope: "thread-1",
    });
    const legacyKey = key.replace(
      "actweave:composer-draft:v1",
      "deerflow:composer-draft:v1",
    );
    const draft = { version: 1, text: "Continue", skillName: null };
    const { storage, values } = memoryStorage({
      [legacyKey]: JSON.stringify(draft),
    });

    expect(key.startsWith("actweave:composer-draft:v1:")).toBe(true);
    expect(readComposerDraft(storage, key)).toEqual({
      text: "Continue",
      skillName: null,
    });
    expect(values.get(key)).toBe(JSON.stringify(draft));
    expect(values.has(legacyKey)).toBe(false);
  });

  test("writes the new key and clear removes both key generations", () => {
    const key = "actweave:composer-draft:v1:a:p:main:thread";
    const legacyKey = "deerflow:composer-draft:v1:a:p:main:thread";
    const { storage, values } = memoryStorage({ [legacyKey]: "legacy" });

    writeComposerDraft(storage, key, { text: "New", skillName: null });
    expect(values.has(legacyKey)).toBe(false);
    expect(values.has(key)).toBe(true);

    clearComposerDraft(storage, key);
    expect(values.size).toBe(0);
  });

  test("uses valid legacy JSON only when the current draft is malformed", () => {
    const key = "actweave:composer-draft:v1:a:p:main:thread";
    const legacyKey = "deerflow:composer-draft:v1:a:p:main:thread";
    const legacyDraft = { version: 1, text: "Recovered", skillName: null };
    const { storage, values } = memoryStorage({
      [key]: "{malformed",
      [legacyKey]: JSON.stringify(legacyDraft),
    });

    expect(readComposerDraft(storage, key)).toEqual({
      text: "Recovered",
      skillName: null,
    });
    expect(Object.fromEntries(values)).toEqual({
      [key]: JSON.stringify(legacyDraft),
    });

    values.delete(key);
    values.set(legacyKey, "{malformed");
    expect(readComposerDraft(storage, key)).toBeNull();
    expect(Object.fromEntries(values)).toEqual({
      [legacyKey]: "{malformed",
    });
  });
});
