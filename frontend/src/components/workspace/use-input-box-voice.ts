"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import {
  getVoiceInputErrorMessage,
  getVoiceInputToggleAction,
  type VoiceInputErrorMessages,
} from "@/core/voice-input/interaction";
import {
  appendSpeechTranscript,
  getSpeechRecognitionConstructor,
  getSpeechRecognitionLanguage,
  mapSpeechRecognitionError,
  readSpeechRecognitionTranscript,
  shouldRestartSpeechRecognition,
  type BrowserSpeechRecognition,
  type SpeechRecognitionErrorKind,
} from "@/core/voice-input/speech-recognition";

const VOICE_RECOGNITION_RESTART_DELAY_MS = 150;

type VoiceRecognitionStartOptions = {
  focusAfterStart?: boolean;
};

export type InputBoxVoiceCallbacks = {
  focusInput: () => void;
  onBeforeStart: () => void;
  setInput: (value: string) => void;
};

export type InputBoxVoiceMessages = VoiceInputErrorMessages & {
  unsupported: string;
};

export type UseInputBoxVoiceOptions = {
  callbacks: InputBoxVoiceCallbacks;
  composerLocked: boolean;
  draftKey: string;
  locale: string;
  messages: InputBoxVoiceMessages;
  text: string;
  threadId: string;
};

export type UseInputBoxVoiceResult = {
  abort: () => void;
  listening: boolean;
  supported: boolean;
  toggle: () => void;
};

export function useInputBoxVoice({
  callbacks,
  composerLocked,
  draftKey,
  locale,
  messages,
  text,
  threadId,
}: UseInputBoxVoiceOptions): UseInputBoxVoiceResult {
  const { focusInput, onBeforeStart, setInput } = callbacks;
  const recognitionRef = useRef<BrowserSpeechRecognition | null>(null);
  const baseTextRef = useRef("");
  const latestTextRef = useRef("");
  const lastErrorKindRef = useRef<SpeechRecognitionErrorKind | null>(null);
  const stopRequestedRef = useRef(false);
  const restartTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const startRecognitionRef = useRef<
    ((options?: VoiceRecognitionStartOptions) => boolean) | null
  >(null);
  const [listening, setListening] = useState(false);
  const recognitionConstructor = useMemo(
    () =>
      typeof window === "undefined"
        ? null
        : getSpeechRecognitionConstructor(window),
    [],
  );
  const errorMessages = useMemo<VoiceInputErrorMessages>(
    () => ({
      failed: messages.failed,
      microphoneUnavailable: messages.microphoneUnavailable,
      networkError: messages.networkError,
      noSpeech: messages.noSpeech,
      permissionDenied: messages.permissionDenied,
      unsupportedLanguage: messages.unsupportedLanguage,
    }),
    [
      messages.failed,
      messages.microphoneUnavailable,
      messages.networkError,
      messages.noSpeech,
      messages.permissionDenied,
      messages.unsupportedLanguage,
    ],
  );
  const clearRestartTimer = useCallback(() => {
    if (restartTimerRef.current === null) {
      return;
    }
    clearTimeout(restartTimerRef.current);
    restartTimerRef.current = null;
  }, []);
  const cleanupRecognition = useCallback(
    (
      recognition: BrowserSpeechRecognition | null,
      options: { keepListening?: boolean } = {},
    ) => {
      clearRestartTimer();
      if (!recognition) {
        if (!options.keepListening) {
          lastErrorKindRef.current = null;
          stopRequestedRef.current = false;
          setListening(false);
        }
        return;
      }
      recognition.onend = null;
      recognition.onerror = null;
      recognition.onresult = null;
      if (recognitionRef.current === recognition) {
        recognitionRef.current = null;
      }
      if (!options.keepListening) {
        lastErrorKindRef.current = null;
        stopRequestedRef.current = false;
        setListening(false);
      }
    },
    [clearRestartTimer],
  );
  const abort = useCallback(() => {
    const recognition = recognitionRef.current;
    stopRequestedRef.current = true;
    if (!recognition) {
      cleanupRecognition(null);
      return;
    }
    cleanupRecognition(recognition);
    try {
      recognition.abort();
    } catch {
      // Browser implementations can throw when the recognizer already ended.
    }
  }, [cleanupRecognition]);

  useEffect(() => {
    return () => abort();
  }, [abort, draftKey, threadId]);

  const startRecognition = useCallback(
    (options: VoiceRecognitionStartOptions = {}) => {
      if (composerLocked || !recognitionConstructor) {
        return false;
      }

      const recognition = new recognitionConstructor();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = getSpeechRecognitionLanguage(locale);
      recognition.maxAlternatives = 1;
      lastErrorKindRef.current = null;
      latestTextRef.current = baseTextRef.current;
      recognitionRef.current = recognition;

      recognition.onresult = (event) => {
        if (recognitionRef.current !== recognition) {
          return;
        }
        const transcript = readSpeechRecognitionTranscript(event.results).text;
        const nextValue = appendSpeechTranscript(
          baseTextRef.current,
          transcript,
        );
        latestTextRef.current = nextValue;
        setInput(nextValue);
      };
      recognition.onerror = (event) => {
        const errorKind = mapSpeechRecognitionError(event.error);
        lastErrorKindRef.current = errorKind;
        if (
          !stopRequestedRef.current &&
          shouldRestartSpeechRecognition(errorKind)
        ) {
          return;
        }

        const message = getVoiceInputErrorMessage(errorKind, errorMessages);
        if (message) {
          toast.error(message);
        }
      };
      recognition.onend = () => {
        const shouldRestart =
          recognitionRef.current === recognition &&
          !stopRequestedRef.current &&
          shouldRestartSpeechRecognition(lastErrorKindRef.current);
        if (shouldRestart) {
          baseTextRef.current = latestTextRef.current;
          cleanupRecognition(recognition, { keepListening: true });
          restartTimerRef.current = setTimeout(() => {
            restartTimerRef.current = null;
            if (stopRequestedRef.current) {
              cleanupRecognition(null);
              return;
            }
            const restarted = startRecognitionRef.current?.() ?? false;
            if (!restarted) {
              cleanupRecognition(null);
            }
          }, VOICE_RECOGNITION_RESTART_DELAY_MS);
          return;
        }
        cleanupRecognition(recognition);
      };

      setListening(true);
      try {
        recognition.start();
        if (options.focusAfterStart) {
          requestAnimationFrame(() => {
            focusInput();
          });
        }
        return true;
      } catch {
        cleanupRecognition(recognition);
        toast.error(messages.failed);
        return false;
      }
    },
    [
      cleanupRecognition,
      composerLocked,
      errorMessages,
      focusInput,
      locale,
      messages.failed,
      recognitionConstructor,
      setInput,
    ],
  );

  useEffect(() => {
    startRecognitionRef.current = startRecognition;
  }, [startRecognition]);

  const stop = useCallback(() => {
    const recognition = recognitionRef.current;
    stopRequestedRef.current = true;
    if (!recognition) {
      cleanupRecognition(null);
      return;
    }
    try {
      recognition.stop();
    } catch {
      cleanupRecognition(recognition);
    }
  }, [cleanupRecognition]);

  const toggle = useCallback(() => {
    const action = getVoiceInputToggleAction({
      composerDisabled: composerLocked,
      listening,
      supported: recognitionConstructor !== null,
    });
    if (action === "stop") {
      stop();
      return;
    }
    if (action === "ignore") {
      return;
    }
    if (action === "report_unsupported") {
      toast.error(messages.unsupported);
      return;
    }

    onBeforeStart();
    stopRequestedRef.current = false;
    baseTextRef.current = text;
    latestTextRef.current = baseTextRef.current;
    startRecognition({ focusAfterStart: true });
  }, [
    composerLocked,
    listening,
    messages.unsupported,
    onBeforeStart,
    recognitionConstructor,
    startRecognition,
    stop,
    text,
  ]);

  useEffect(() => {
    if (composerLocked && listening) {
      abort();
    }
  }, [abort, composerLocked, listening]);

  return {
    abort,
    listening,
    supported: recognitionConstructor !== null,
    toggle,
  };
}
