import { describe, expect, rs, test } from "@rstest/core";
import type { QueryClient } from "@tanstack/react-query";

import {
  notifySkillDraftVersionCreated,
  skillVersionConflictHasLatestServerState,
  skillVersionDraftMatchesSubmittedChanges,
  skillVersionSaveIsPending,
  skillVersionWorkbenchTabForKey,
} from "@/components/projects/assets/skill-version-workbench";
import { skillWorkbenchTabVariant } from "@/components/projects/assets/skill-workbench-tabs";
import type { SkillFileChange } from "@/core/shared-assets";
import { invalidateProjectSkillConflictQueries } from "@/core/shared-assets/hooks";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const ASSET_ID = "33333333-3333-4333-8333-333333333333";
const SOURCE_VERSION_ID = "44444444-4444-4444-8444-444444444444";

describe("Skill version conflict recovery", () => {
  test("focuses Runtime credentials after saving a Draft with declarations", () => {
    const events: unknown[][] = [];
    notifySkillDraftVersionCreated((...args) => events.push(args), {
      id: SOURCE_VERSION_ID,
      secret_requirements: [{ name: "API_KEY", optional: false }],
    });
    expect(events).toEqual([[SOURCE_VERSION_ID, { focusCredentials: true }]]);
  });

  test("does not force Runtime credentials after saving a Draft without declarations", () => {
    const events: unknown[][] = [];
    notifySkillDraftVersionCreated((...args) => events.push(args), {
      id: SOURCE_VERSION_ID,
      secret_requirements: [],
    });
    expect(events).toEqual([[SOURCE_VERSION_ID, { focusCredentials: false }]]);
  });

  test("uses a high-contrast selected tab variant", () => {
    expect(skillWorkbenchTabVariant(true)).toBe("default");
    expect(skillWorkbenchTabVariant(false)).toBe("ghost");
  });

  test("maps the complete horizontal tab keyboard contract", () => {
    expect(skillVersionWorkbenchTabForKey("files", "ArrowRight")).toBe(
      "secrets",
    );
    expect(skillVersionWorkbenchTabForKey("secrets", "ArrowLeft")).toBe(
      "files",
    );
    expect(skillVersionWorkbenchTabForKey("secrets", "Home")).toBe("files");
    expect(skillVersionWorkbenchTabForKey("files", "End")).toBe("secrets");
    expect(skillVersionWorkbenchTabForKey("files", "Enter")).toBeNull();
  });

  test("accepts only a newer authoritative revision for the same asset and immutable source version", () => {
    const recovery = {
      assetId: ASSET_ID,
      assetVersion: 7,
      sourceVersionId: SOURCE_VERSION_ID,
    };

    expect(
      skillVersionConflictHasLatestServerState(
        recovery,
        { id: ASSET_ID, version: 7 },
        { id: SOURCE_VERSION_ID },
      ),
    ).toBe(false);
    expect(
      skillVersionConflictHasLatestServerState(
        recovery,
        { id: ASSET_ID, version: 8 },
        { id: SOURCE_VERSION_ID },
      ),
    ).toBe(true);
    expect(
      skillVersionConflictHasLatestServerState(
        recovery,
        { id: "another-asset", version: 8 },
        { id: SOURCE_VERSION_ID },
      ),
    ).toBe(false);
    expect(
      skillVersionConflictHasLatestServerState(
        recovery,
        { id: ASSET_ID, version: 8 },
        { id: "another-source-version" },
      ),
    ).toBe(false);
  });

  test("invalidates the Skill catalog and exact asset version-history roots", async () => {
    const invalidateQueries = rs.fn(() => Promise.resolve());
    const queryClient = { invalidateQueries } as unknown as QueryClient;

    await invalidateProjectSkillConflictQueries(
      queryClient,
      ACCOUNT_ID,
      PROJECT_ID,
      ASSET_ID,
    );

    expect(invalidateQueries).toHaveBeenCalledTimes(2);
    expect(invalidateQueries).toHaveBeenNthCalledWith(1, {
      queryKey: [
        "account",
        ACCOUNT_ID,
        "shared-assets",
        "project",
        PROJECT_ID,
        "skills",
      ],
      exact: true,
    });
    expect(invalidateQueries).toHaveBeenNthCalledWith(2, {
      queryKey: [
        "account",
        ACCOUNT_ID,
        "shared-assets",
        "project",
        PROJECT_ID,
        "skills",
        "asset",
        ASSET_ID,
        "versions",
      ],
      exact: true,
    });
  });

  test("keeps a successful save pending until the authoritative asset revision catches up", () => {
    const pending = {
      assetId: ASSET_ID,
      assetVersion: 8,
    };

    expect(
      skillVersionSaveIsPending(pending, { id: ASSET_ID, version: 7 }),
    ).toBe(true);
    expect(
      skillVersionSaveIsPending(pending, { id: ASSET_ID, version: 8 }),
    ).toBe(false);
    expect(
      skillVersionSaveIsPending(pending, {
        id: "another-asset",
        version: 7,
      }),
    ).toBe(false);
  });

  test("clears only the exact changes submitted by the successful save", () => {
    const submittedChange = {
      op: "replace" as const,
      path: "SKILL.md",
      content: "submitted content",
      media_type: "text/markdown",
    } satisfies SkillFileChange;
    const submitted: SkillFileChange[] = [submittedChange];

    expect(
      skillVersionDraftMatchesSubmittedChanges(submitted, [
        { ...submittedChange },
      ]),
    ).toBe(true);
    expect(
      skillVersionDraftMatchesSubmittedChanges(submitted, [
        { ...submittedChange, content: "edited while saving" },
      ]),
    ).toBe(false);
    expect(
      skillVersionDraftMatchesSubmittedChanges(submitted, [
        ...submitted,
        {
          op: "create" as const,
          path: "guide.md",
          content: "new file",
          media_type: "text/markdown",
        },
      ]),
    ).toBe(false);
  });
});
