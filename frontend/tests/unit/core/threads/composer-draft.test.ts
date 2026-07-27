import { describe, expect, it } from "@rstest/core";

import {
  buildComposerDraftKey,
  clearComposerDraft,
  readComposerDraft,
  resolveComposerDraft,
  writeComposerDraft,
  type ComposerDraftStorage,
} from "@/core/threads/composer-draft";

class MemoryStorage implements ComposerDraftStorage {
  readonly values = new Map<string, string>();
  throwOnRead = false;
  throwOnWrite = false;

  getItem(key: string) {
    if (this.throwOnRead) throw new DOMException("Storage unavailable");
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string) {
    if (this.throwOnWrite) throw new DOMException("Storage full");
    this.values.set(key, value);
  }

  removeItem(key: string) {
    if (this.throwOnWrite) throw new DOMException("Storage unavailable");
    this.values.delete(key);
  }
}

describe("composer draft storage", () => {
  it("isolates drafts by account, project, agent, and conversation", () => {
    const base = {
      accountId: "account:1",
      projectId: "project/1",
      agentName: "lead-agent",
      conversationScope: "new",
    };
    const key = buildComposerDraftKey(base);

    expect(key).not.toBe(
      buildComposerDraftKey({ ...base, accountId: "account:2" }),
    );
    expect(key).not.toBe(
      buildComposerDraftKey({ ...base, projectId: "project/2" }),
    );
    expect(key).not.toBe(
      buildComposerDraftKey({ ...base, agentName: "reviewer" }),
    );
    expect(key).not.toBe(
      buildComposerDraftKey({ ...base, conversationScope: "thread/1" }),
    );
    expect(key).toContain("account%3A1");
    expect(key).toContain("project%2F1");
  });

  it("round-trips, removes empty drafts, and tolerates storage failures", () => {
    const storage = new MemoryStorage();
    const key = "draft-key";
    const draft = { text: "Summarize the report", skillName: "analysis" };

    writeComposerDraft(storage, key, draft);
    expect(readComposerDraft(storage, key)).toEqual(draft);

    writeComposerDraft(storage, key, { text: "", skillName: null });
    expect(readComposerDraft(storage, key)).toBeNull();

    storage.throwOnRead = true;
    expect(readComposerDraft(storage, key)).toBeNull();
    storage.throwOnRead = false;
    storage.throwOnWrite = true;
    expect(() => writeComposerDraft(storage, key, draft)).not.toThrow();
    expect(() => clearComposerDraft(storage, key)).not.toThrow();
  });

  it("falls back to editable slash text when a saved skill is unavailable", () => {
    const draft = { text: "Analyze this", skillName: "data-analysis" };

    expect(resolveComposerDraft(draft, new Set(["data-analysis"]))).toEqual(
      draft,
    );
    expect(resolveComposerDraft(draft, new Set())).toEqual({
      text: "/data-analysis Analyze this",
      skillName: null,
    });
  });
});
