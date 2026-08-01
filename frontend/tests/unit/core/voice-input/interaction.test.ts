import { describe, expect, it } from "@rstest/core";

import {
  getVoiceInputButtonState,
  getVoiceInputToggleAction,
} from "@/core/voice-input/interaction";

describe("voice input interaction", () => {
  it("keeps browser-unsupported voice input focusable while exposing disabled semantics", () => {
    expect(
      getVoiceInputButtonState({
        composerDisabled: false,
        supported: false,
      }),
    ).toEqual({
      ariaDisabled: true,
      nativeDisabled: false,
      visuallyDisabled: true,
    });
  });

  it("uses native disabled semantics only when the whole composer is disabled", () => {
    expect(
      getVoiceInputButtonState({
        composerDisabled: true,
        supported: false,
      }),
    ).toEqual({
      ariaDisabled: true,
      nativeDisabled: true,
      visuallyDisabled: true,
    });
    expect(
      getVoiceInputButtonState({
        composerDisabled: true,
        supported: true,
      }),
    ).toEqual({
      ariaDisabled: undefined,
      nativeDisabled: true,
      visuallyDisabled: false,
    });
  });

  it("reports unsupported activation without starting a recognizer", () => {
    expect(
      getVoiceInputToggleAction({
        composerDisabled: false,
        listening: false,
        supported: false,
      }),
    ).toBe("report_unsupported");
    expect(
      getVoiceInputToggleAction({
        composerDisabled: false,
        listening: false,
        supported: true,
      }),
    ).toBe("start");
  });

  it("preserves stop and composer-disabled actions", () => {
    expect(
      getVoiceInputToggleAction({
        composerDisabled: false,
        listening: true,
        supported: true,
      }),
    ).toBe("stop");
    expect(
      getVoiceInputToggleAction({
        composerDisabled: true,
        listening: false,
        supported: true,
      }),
    ).toBe("ignore");
  });
});
