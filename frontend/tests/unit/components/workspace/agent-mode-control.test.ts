import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

const inputBoxSource = readFileSync(
  resolve(process.cwd(), "src/components/workspace/input-box.tsx"),
  "utf8",
);
const sidecarSource = readFileSync(
  resolve(process.cwd(), "src/components/workspace/sidecar/sidecar-panel.tsx"),
  "utf8",
);
const hooksSource = readFileSync(
  resolve(process.cwd(), "src/core/threads/hooks.ts"),
  "utf8",
);

describe("single agent mode control", () => {
  test("removes the standalone reasoning-effort control", () => {
    expect(inputBoxSource).not.toContain("handleReasoningEffortSelect");
    expect(inputBoxSource).not.toContain("supportReasoningEffort");
    expect(inputBoxSource).not.toContain("t.inputBox.reasoningEffort");
  });

  test("shares one mode preset across composers and runtime submission", () => {
    expect(inputBoxSource).toContain('from "@/core/threads/agent-mode"');
    expect(sidecarSource).toContain('from "@/core/threads/agent-mode"');
    expect(hooksSource).toContain('from "@/core/threads/agent-mode"');
    expect(hooksSource).not.toContain("context.reasoning_effort ??");
  });
});
