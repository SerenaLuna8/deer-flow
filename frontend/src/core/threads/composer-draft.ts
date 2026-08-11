import {
  readMigratedStorageValue,
  removeMigratedStorageValue,
  writeMigratedStorageValue,
} from "@/core/storage/brand-key-migration";

const COMPOSER_DRAFT_VERSION = 1;
const COMPOSER_DRAFT_PREFIX = "actweave:composer-draft:v1";
const LEGACY_COMPOSER_DRAFT_PREFIX = "deerflow:composer-draft:v1";

export type ComposerDraft = {
  text: string;
  skillName: string | null;
};

export type ComposerDraftStorage = Pick<
  Storage,
  "getItem" | "setItem" | "removeItem"
>;

export function getSessionComposerDraftStorage(): ComposerDraftStorage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function buildComposerDraftKey({
  accountId,
  projectId,
  agentName,
  conversationScope,
}: {
  accountId: string;
  projectId: string;
  agentName?: string | null;
  conversationScope: string;
}) {
  return [
    COMPOSER_DRAFT_PREFIX,
    encodeURIComponent(accountId || "anonymous"),
    encodeURIComponent(projectId || "no-project"),
    encodeURIComponent(agentName ?? "lead-agent"),
    encodeURIComponent(conversationScope),
  ].join(":");
}

function legacyComposerDraftKeys(key: string): string[] {
  if (!key.startsWith(`${COMPOSER_DRAFT_PREFIX}:`)) {
    return [];
  }
  return [key.replace(COMPOSER_DRAFT_PREFIX, LEGACY_COMPOSER_DRAFT_PREFIX)];
}

function isSerializedComposerDraft(value: string): boolean {
  try {
    const parsed = JSON.parse(value) as Record<string, unknown>;
    return (
      parsed.version === COMPOSER_DRAFT_VERSION &&
      typeof parsed.text === "string" &&
      (parsed.skillName === null || typeof parsed.skillName === "string")
    );
  } catch {
    return false;
  }
}

export function readComposerDraft(
  storage: ComposerDraftStorage | null | undefined,
  key: string,
): ComposerDraft | null {
  try {
    if (!storage) return null;
    const raw = readMigratedStorageValue(
      storage,
      key,
      legacyComposerDraftKeys(key),
      isSerializedComposerDraft,
    );
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (
      parsed.version !== COMPOSER_DRAFT_VERSION ||
      typeof parsed.text !== "string" ||
      !(parsed.skillName === null || typeof parsed.skillName === "string")
    ) {
      return null;
    }
    return { text: parsed.text, skillName: parsed.skillName };
  } catch {
    return null;
  }
}

export function writeComposerDraft(
  storage: ComposerDraftStorage | null | undefined,
  key: string,
  draft: ComposerDraft,
) {
  try {
    if (!storage) return;
    if (!draft.text && !draft.skillName) {
      removeMigratedStorageValue(storage, key, legacyComposerDraftKeys(key));
      return;
    }
    writeMigratedStorageValue(
      storage,
      key,
      JSON.stringify({ version: COMPOSER_DRAFT_VERSION, ...draft }),
      legacyComposerDraftKeys(key),
    );
  } catch {
    // Storage may be disabled or full. Drafting must remain usable.
  }
}

export function clearComposerDraft(
  storage: ComposerDraftStorage | null | undefined,
  key: string,
) {
  try {
    if (!storage) return;
    removeMigratedStorageValue(storage, key, legacyComposerDraftKeys(key));
  } catch {
    // Sending must remain usable when browser storage is unavailable.
  }
}

export function resolveComposerDraft(
  draft: ComposerDraft,
  enabledSkillNames: ReadonlySet<string>,
): ComposerDraft {
  if (!draft.skillName || enabledSkillNames.has(draft.skillName)) {
    return draft;
  }
  return {
    text: `/${draft.skillName}${draft.text ? ` ${draft.text}` : ""}`,
    skillName: null,
  };
}
