import {
  afterEach,
  beforeEach,
  describe,
  expect,
  rs,
  test,
} from "@rstest/core";
import { toast } from "sonner";

import {
  useInputBoxVoice as invokeInputBoxVoice,
  type InputBoxVoiceCallbacks,
  type UseInputBoxVoiceOptions,
} from "@/components/workspace/use-input-box-voice";
import type {
  BrowserSpeechRecognition,
  SpeechRecognitionErrorEventLike,
  SpeechRecognitionEventLike,
} from "@/core/voice-input/speech-recognition";

type TestEffect = () => void | (() => void);

const refs: Array<{ current: unknown }> = [];
const stateValues: unknown[] = [];
let pendingEffects: TestEffect[] = [];
let refCursor = 0;
let stateCursor = 0;

rs.mock("react", () => ({
  useCallback: <T extends (...args: never[]) => unknown>(callback: T) =>
    callback,
  useEffect: (effect: TestEffect) => {
    pendingEffects.push(effect);
  },
  useMemo: <T>(factory: () => T) => factory(),
  useRef: <T>(initialValue: T) => {
    const index = refCursor;
    refCursor += 1;
    refs[index] ??= { current: initialValue };
    return refs[index] as { current: T };
  },
  useState: <T>(initialValue: T | (() => T)) => {
    const index = stateCursor;
    stateCursor += 1;
    if (stateValues.length <= index) {
      stateValues[index] =
        typeof initialValue === "function"
          ? (initialValue as () => T)()
          : initialValue;
    }
    const setValue = (nextValue: T | ((current: T) => T)) => {
      const current = stateValues[index] as T;
      stateValues[index] =
        typeof nextValue === "function"
          ? (nextValue as (value: T) => T)(current)
          : nextValue;
    };
    return [stateValues[index] as T, setValue] as const;
  },
}));
rs.mock("sonner", () => ({
  toast: {
    error: rs.fn(),
  },
}));

class FakeSpeechRecognition implements BrowserSpeechRecognition {
  static instances: FakeSpeechRecognition[] = [];

  continuous = false;
  interimResults = false;
  lang = "";
  maxAlternatives = 0;
  onend: (() => void) | null = null;
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null = null;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null = null;
  start = rs.fn(() => undefined);
  stop = rs.fn(() => undefined);
  abort = rs.fn(() => undefined);

  constructor() {
    FakeSpeechRecognition.instances.push(this);
  }
}

const messages = {
  failed: "failed",
  microphoneUnavailable: "microphone unavailable",
  networkError: "network error",
  noSpeech: "no speech",
  permissionDenied: "permission denied",
  unsupported: "unsupported",
  unsupportedLanguage: "unsupported language",
};

let originalAnimationFrame: PropertyDescriptor | undefined;
let originalSetTimeout: PropertyDescriptor | undefined;
let originalWindow: PropertyDescriptor | undefined;

function beginRender() {
  refCursor = 0;
  stateCursor = 0;
  pendingEffects = [];
}

function flushEffects() {
  const effects = pendingEffects;
  pendingEffects = [];
  return effects.flatMap((effect) => {
    const cleanup = effect();
    return typeof cleanup === "function" ? [cleanup] : [];
  });
}

function renderVoice(options: UseInputBoxVoiceOptions) {
  beginRender();
  const result = invokeInputBoxVoice(options);
  const cleanups = flushEffects();
  return { cleanups, result };
}

function createOptions(
  overrides: Partial<UseInputBoxVoiceOptions> = {},
): UseInputBoxVoiceOptions & { callbacks: InputBoxVoiceCallbacks } {
  return {
    callbacks: {
      focusInput: rs.fn(),
      onBeforeStart: rs.fn(),
      setInput: rs.fn(),
    },
    composerLocked: false,
    draftKey: "draft-a",
    locale: "zh-CN",
    messages,
    text: "draft",
    threadId: "thread-a",
    ...overrides,
  };
}

function speechResult(transcript: string): SpeechRecognitionEventLike {
  return {
    results: {
      0: {
        0: { transcript },
        isFinal: true,
        length: 1,
      },
      length: 1,
    },
  };
}

function restoreGlobal(
  key: "requestAnimationFrame" | "setTimeout" | "window",
  descriptor: PropertyDescriptor | undefined,
) {
  if (descriptor) {
    Object.defineProperty(globalThis, key, descriptor);
  } else {
    Reflect.deleteProperty(globalThis, key);
  }
}

beforeEach(() => {
  refs.length = 0;
  stateValues.length = 0;
  pendingEffects = [];
  refCursor = 0;
  stateCursor = 0;
  FakeSpeechRecognition.instances = [];
  rs.clearAllMocks();

  originalAnimationFrame = Object.getOwnPropertyDescriptor(
    globalThis,
    "requestAnimationFrame",
  );
  originalSetTimeout = Object.getOwnPropertyDescriptor(
    globalThis,
    "setTimeout",
  );
  originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "requestAnimationFrame", {
    configurable: true,
    value: (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    },
  });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      SpeechRecognition: FakeSpeechRecognition,
    },
  });
});

afterEach(() => {
  restoreGlobal("requestAnimationFrame", originalAnimationFrame);
  restoreGlobal("setTimeout", originalSetTimeout);
  restoreGlobal("window", originalWindow);
});

describe("useInputBoxVoice", () => {
  test("starts with the browser contract, updates text, focuses, and stops gracefully", () => {
    const options = createOptions();
    let { result } = renderVoice(options);

    expect(result.supported).toBe(true);
    expect(result.listening).toBe(false);
    result.toggle();

    const recognition = FakeSpeechRecognition.instances[0]!;
    expect(options.callbacks.onBeforeStart).toHaveBeenCalledTimes(1);
    expect(recognition.continuous).toBe(true);
    expect(recognition.interimResults).toBe(true);
    expect(recognition.lang).toBe("zh-CN");
    expect(recognition.maxAlternatives).toBe(1);
    expect(recognition.start).toHaveBeenCalledTimes(1);
    expect(options.callbacks.focusInput).toHaveBeenCalledTimes(1);

    recognition.onresult?.(speechResult(" hello world "));
    expect(options.callbacks.setInput).toHaveBeenCalledWith(
      "draft hello world",
    );

    ({ result } = renderVoice(options));
    expect(result.listening).toBe(true);
    const onend = recognition.onend;
    result.toggle();
    expect(recognition.stop).toHaveBeenCalledTimes(1);
    expect(recognition.abort).not.toHaveBeenCalled();

    onend?.();
    ({ result } = renderVoice(options));
    expect(result.listening).toBe(false);
  });

  test("aborts through the public API, composer lock, and lifecycle cleanup", () => {
    const options = createOptions();
    let { cleanups, result } = renderVoice(options);
    result.toggle();
    const firstRecognition = FakeSpeechRecognition.instances[0]!;

    result.abort();
    expect(firstRecognition.abort).toHaveBeenCalledTimes(1);
    expect(firstRecognition.stop).not.toHaveBeenCalled();
    expect(firstRecognition.onend).toBeNull();

    ({ result } = renderVoice(options));
    result.toggle();
    const secondRecognition = FakeSpeechRecognition.instances[1]!;
    renderVoice({ ...options, composerLocked: true });
    expect(secondRecognition.abort).toHaveBeenCalledTimes(1);
    expect(secondRecognition.stop).not.toHaveBeenCalled();

    ({ cleanups, result } = renderVoice(options));
    result.toggle();
    const thirdRecognition = FakeSpeechRecognition.instances[2]!;
    cleanups[0]?.();
    expect(thirdRecognition.abort).toHaveBeenCalledTimes(1);
    expect(thirdRecognition.stop).not.toHaveBeenCalled();
  });

  test("restarts no-speech endings after 150ms without refocusing", () => {
    const scheduled: Array<{
      callback: () => void;
      delay: number | undefined;
    }> = [];
    Object.defineProperty(globalThis, "setTimeout", {
      configurable: true,
      value: (callback: () => void, delay?: number) => {
        scheduled.push({ callback, delay });
        return 1;
      },
    });
    const options = createOptions();
    const { result } = renderVoice(options);
    result.toggle();
    const firstRecognition = FakeSpeechRecognition.instances[0]!;
    const onend = firstRecognition.onend;

    firstRecognition.onresult?.(speechResult(" hello "));
    firstRecognition.onerror?.({ error: "no-speech" });
    expect(toast.error).not.toHaveBeenCalled();
    onend?.();

    expect(scheduled).toHaveLength(1);
    expect(scheduled[0]?.delay).toBe(150);
    scheduled[0]?.callback();
    const secondRecognition = FakeSpeechRecognition.instances[1]!;
    expect(secondRecognition.start).toHaveBeenCalledTimes(1);
    expect(options.callbacks.focusInput).toHaveBeenCalledTimes(1);

    secondRecognition.onresult?.(speechResult(" again "));
    expect(options.callbacks.setInput).toHaveBeenLastCalledWith(
      "draft hello again",
    );
  });

  test("reports unsupported browsers without running start callbacks", () => {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: {},
    });
    const options = createOptions();
    const { result } = renderVoice(options);

    expect(result.supported).toBe(false);
    result.toggle();
    expect(toast.error).toHaveBeenCalledWith("unsupported");
    expect(options.callbacks.onBeforeStart).not.toHaveBeenCalled();
  });
});
