import { describe, expect, test } from "@rstest/core";

import {
  resolveSkillSecretEditorAccess,
  skillSecretAutonomousFromInjectionMode,
  skillSecretFeedbackMessage,
  skillFrontmatterRequestIsCurrent,
  skillFrontmatterResponseIsCurrent,
  skillSecretDraftAfterPatch,
  skillSecretInjectionModeFromAutonomous,
  skillSecretNameFocusDecision,
  shouldShowSkillSecretInjectionSettings,
} from "@/components/projects/assets/skill-secret-declarations-editor";
import { zhCN } from "@/core/i18n/locales/zh-CN";

describe("Skill secret declaration editor product states", () => {
  test("separates draft editing from permission to begin a new version", () => {
    expect(
      resolveSkillSecretEditorAccess({
        editable: false,
        canEdit: true,
        canBeginEdit: true,
      }),
    ).toEqual({ editable: false, canBeginEdit: true });

    expect(
      resolveSkillSecretEditorAccess({
        canEdit: true,
        canBeginEdit: true,
      }),
    ).toEqual({ editable: true, canBeginEdit: false });
  });

  test("distinguishes source recognition from an unsaved form patch", () => {
    const copy = zhCN.skills.secrets;

    expect(skillSecretFeedbackMessage(copy, "source", 2)).toBe(
      "已从 SKILL.md 识别 2 个环境变量",
    );
    expect(skillSecretFeedbackMessage(copy, "draft", 2)).toBe(
      "已写入 SKILL.md，修改尚未保存",
    );
  });

  test("maps the two injection choices to secrets-autonomous without inversion", () => {
    expect(skillSecretInjectionModeFromAutonomous(true)).toBe("automatic");
    expect(skillSecretInjectionModeFromAutonomous(false)).toBe("explicit");
    expect(skillSecretAutonomousFromInjectionMode("automatic")).toBe(true);
    expect(skillSecretAutonomousFromInjectionMode("explicit")).toBe(false);
  });

  test("hides injection settings until at least one variable is declared", () => {
    expect(shouldShowSkillSecretInjectionSettings(0)).toBe(false);
    expect(shouldShowSkillSecretInjectionSettings(1)).toBe(true);
  });

  test("focuses the variable input only after the empty-state CTA enters edit mode", () => {
    expect(
      skillSecretNameFocusDecision({
        editable: true,
        focusRequested: false,
        inputReady: true,
      }),
    ).toEqual({ shouldFocus: false, keepRequest: false });
    expect(
      skillSecretNameFocusDecision({
        editable: false,
        focusRequested: true,
        inputReady: true,
      }),
    ).toEqual({ shouldFocus: false, keepRequest: true });
    expect(
      skillSecretNameFocusDecision({
        editable: true,
        focusRequested: true,
        inputReady: false,
      }),
    ).toEqual({ shouldFocus: false, keepRequest: true });
    expect(
      skillSecretNameFocusDecision({
        editable: true,
        focusRequested: true,
        inputReady: true,
      }),
    ).toEqual({ shouldFocus: true, keepRequest: false });
  });
});

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
        name: "provider_key",
        targetEnv: "OPENAI_API_KEY",
        optional: true,
      }),
    ).toEqual({
      name: "provider_key",
      targetEnv: "OPENAI_API_KEY",
      optional: true,
    });
    expect(
      skillSecretDraftAfterPatch({
        patchSucceeded: true,
        name: "provider_key",
        targetEnv: "OPENAI_API_KEY",
        optional: true,
      }),
    ).toEqual({ name: "", targetEnv: "", optional: false });
  });
});
