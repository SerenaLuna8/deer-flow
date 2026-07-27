const COMPOSER_DRAFT_VERSION = 1;
const COMPOSER_DRAFT_PREFIX = "deerflow:composer-draft:v1";

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

export function readComposerDraft(
  storage: ComposerDraftStorage | null | undefined,
  key: string,
): ComposerDraft | null {
  try {
    const raw = storage?.getItem(key);
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
      storage.removeItem(key);
      return;
    }
    storage.setItem(
      key,
      JSON.stringify({ version: COMPOSER_DRAFT_VERSION, ...draft }),
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
    storage?.removeItem(key);
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
