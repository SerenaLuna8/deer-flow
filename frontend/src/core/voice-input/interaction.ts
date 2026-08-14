import type { SpeechRecognitionErrorKind } from "./speech-recognition";

export type VoiceInputToggleAction =
  | "ignore"
  | "report_unsupported"
  | "start"
  | "stop";

export type VoiceInputErrorMessages = {
  failed: string;
  microphoneUnavailable: string;
  networkError: string;
  noSpeech: string;
  permissionDenied: string;
  unsupportedLanguage: string;
};

export function getVoiceInputErrorMessage(
  kind: SpeechRecognitionErrorKind,
  messages: VoiceInputErrorMessages,
): string | null {
  switch (kind) {
    case "permission_denied":
      return messages.permissionDenied;
    case "microphone_unavailable":
      return messages.microphoneUnavailable;
    case "unsupported_language":
      return messages.unsupportedLanguage;
    case "network":
      return messages.networkError;
    case "no_speech":
      return messages.noSpeech;
    case "cancelled":
      return null;
    default:
      return messages.failed;
  }
}

export function getVoiceInputButtonState({
  composerDisabled,
  supported,
}: {
  composerDisabled: boolean;
  supported: boolean;
}): {
  ariaDisabled: true | undefined;
  nativeDisabled: boolean;
  visuallyDisabled: boolean;
} {
  return {
    ariaDisabled: supported ? undefined : true,
    nativeDisabled: composerDisabled,
    visuallyDisabled: !supported,
  };
}

export function getVoiceInputToggleAction({
  composerDisabled,
  listening,
  supported,
}: {
  composerDisabled: boolean;
  listening: boolean;
  supported: boolean;
}): VoiceInputToggleAction {
  if (listening) {
    return "stop";
  }
  if (composerDisabled) {
    return "ignore";
  }
  if (!supported) {
    return "report_unsupported";
  }
  return "start";
}
