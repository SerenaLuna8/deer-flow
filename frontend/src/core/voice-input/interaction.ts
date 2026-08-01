export type VoiceInputToggleAction =
  | "ignore"
  | "report_unsupported"
  | "start"
  | "stop";

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
