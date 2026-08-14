import { describe, expect, test } from "@rstest/core";

import {
  getVoiceInputErrorMessage,
  getVoiceInputToggleAction,
  type VoiceInputErrorMessages,
} from "@/core/voice-input/interaction";
import {
  getSpeechRecognitionLanguage,
  mapSpeechRecognitionError,
  readSpeechRecognitionTranscript,
  shouldRestartSpeechRecognition,
} from "@/core/voice-input/speech-recognition";

const errorMessages: VoiceInputErrorMessages = {
  failed: "failed",
  microphoneUnavailable: "microphone unavailable",
  networkError: "network error",
  noSpeech: "no speech",
  permissionDenied: "permission denied",
  unsupportedLanguage: "unsupported language",
};

describe("voice input interaction", () => {
  test("projects recognition errors to localized messages without reporting cancellation", () => {
    expect(getVoiceInputErrorMessage("permission_denied", errorMessages)).toBe(
      "permission denied",
    );
    expect(
      getVoiceInputErrorMessage("microphone_unavailable", errorMessages),
    ).toBe("microphone unavailable");
    expect(
      getVoiceInputErrorMessage("unsupported_language", errorMessages),
    ).toBe("unsupported language");
    expect(getVoiceInputErrorMessage("network", errorMessages)).toBe(
      "network error",
    );
    expect(getVoiceInputErrorMessage("no_speech", errorMessages)).toBe(
      "no speech",
    );
    expect(getVoiceInputErrorMessage("cancelled", errorMessages)).toBeNull();
    expect(getVoiceInputErrorMessage("unknown", errorMessages)).toBe("failed");
  });

  test("keeps stop precedence while ignoring locked or unsupported starts", () => {
    expect(
      getVoiceInputToggleAction({
        composerDisabled: true,
        listening: true,
        supported: false,
      }),
    ).toBe("stop");
    expect(
      getVoiceInputToggleAction({
        composerDisabled: true,
        listening: false,
        supported: true,
      }),
    ).toBe("ignore");
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
});

describe("speech recognition projections", () => {
  test("maps browser errors and only restarts after normal or no-speech endings", () => {
    expect(mapSpeechRecognitionError("aborted")).toBe("cancelled");
    expect(mapSpeechRecognitionError("audio-capture")).toBe(
      "microphone_unavailable",
    );
    expect(mapSpeechRecognitionError("not-allowed")).toBe("permission_denied");
    expect(mapSpeechRecognitionError("service-not-allowed")).toBe(
      "permission_denied",
    );
    expect(mapSpeechRecognitionError("language-not-supported")).toBe(
      "unsupported_language",
    );
    expect(mapSpeechRecognitionError("network")).toBe("network");
    expect(mapSpeechRecognitionError("no-speech")).toBe("no_speech");
    expect(mapSpeechRecognitionError("bad-grammar")).toBe("unknown");

    expect(shouldRestartSpeechRecognition(null)).toBe(true);
    expect(shouldRestartSpeechRecognition("no_speech")).toBe(true);
    expect(shouldRestartSpeechRecognition("network")).toBe(false);
    expect(shouldRestartSpeechRecognition("cancelled")).toBe(false);
  });

  test("keeps locale and transcript normalization stable", () => {
    expect(getSpeechRecognitionLanguage("zh-CN")).toBe("zh-CN");
    expect(getSpeechRecognitionLanguage("en-us")).toBe("en-US");
    expect(getSpeechRecognitionLanguage("invalid locale")).toBe("en-US");
    expect(
      readSpeechRecognitionTranscript({
        0: { 0: { transcript: " hello " }, isFinal: true, length: 1 },
        1: { 0: { transcript: " world " }, isFinal: false, length: 1 },
        length: 2,
      }),
    ).toEqual({
      finalText: "hello",
      interimText: "world",
      text: "hello world",
    });
  });
});
