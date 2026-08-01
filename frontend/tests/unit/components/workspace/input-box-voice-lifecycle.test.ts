import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "@rstest/core";

const inputBoxSource = readFileSync(
  resolve(process.cwd(), "src/components/workspace/input-box.tsx"),
  "utf8",
);

describe("workspace voice input lifecycle", () => {
  it("detaches callbacks before aborting the active recognizer", () => {
    expect(inputBoxSource).toContain("recognition.onend = null;");
    expect(inputBoxSource).toContain("recognition.onerror = null;");
    expect(inputBoxSource).toContain("recognition.onresult = null;");
    expect(inputBoxSource).toContain("recognition.abort();");
  });

  it("aborts before sending or clearing the active draft", () => {
    expect(inputBoxSource).toContain(
      "async (message: PromptInputMessage) => {\n      abortVoiceInput();",
    );
    expect(inputBoxSource).toContain(
      "useLayoutEffect(() => {\n    abortVoiceInput();\n    flushLatestDraft();",
    );
    expect(inputBoxSource).toContain(
      "const applySkillSuggestion = useCallback(\n    (suggestion: SlashSuggestion) => {\n      abortVoiceInput();",
    );
  });

  it("aborts on disabled, thread/project transition, and unmount", () => {
    expect(inputBoxSource).toContain(
      "if (composerLocked && voiceListening) {\n      abortVoiceInput();",
    );
    expect(inputBoxSource).toContain(
      "return () => abortVoiceInput();\n  }, [abortVoiceInput, draftKey, threadId]);",
    );
  });
});
