import { describe, expect, test } from "@rstest/core";

import {
  skillFrontmatterRequestIsCurrent,
  skillFrontmatterResponseIsCurrent,
  skillSecretDraftAfterPatch,
} from "@/components/projects/assets/skill-secret-declarations-editor";

describe("Skill secret declaration editor concurrency", () => {
  test("drops both success and error outcomes from an older generation or source", () => {
    const current = {
      generation: 4,
      currentGeneration: 4,
      sourceContent: "current source",
      currentContent: "current source",
      sourceSha256: "a".repeat(64),
      responseSourceSha256: "a".repeat(64),
    };

    expect(skillFrontmatterResponseIsCurrent(current)).toBe(true);
    expect(
      skillFrontmatterResponseIsCurrent({
        ...current,
        generation: 3,
      }),
    ).toBe(false);
    expect(
      skillFrontmatterResponseIsCurrent({
        ...current,
        currentContent: "newer local source",
      }),
    ).toBe(false);
    expect(
      skillFrontmatterResponseIsCurrent({
        ...current,
        responseSourceSha256: "b".repeat(64),
      }),
    ).toBe(false);

    expect(
      skillFrontmatterRequestIsCurrent({
        generation: 3,
        currentGeneration: 4,
        sourceContent: "older source",
        currentContent: "newer source",
      }),
    ).toBe(false);
  });

  test("keeps the pending form draft when patching fails", () => {
    expect(
      skillSecretDraftAfterPatch({
        patchSucceeded: false,
        name: "OPENAI_API_KEY",
        optional: true,
      }),
    ).toEqual({ name: "OPENAI_API_KEY", optional: true });
    expect(
      skillSecretDraftAfterPatch({
        patchSucceeded: true,
        name: "OPENAI_API_KEY",
        optional: true,
      }),
    ).toEqual({ name: "", optional: false });
  });
});
