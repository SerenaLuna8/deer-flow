import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@rstest/core";

const messageListSource = readFileSync(
  resolve(
    process.cwd(),
    "src/components/workspace/messages/message-list.tsx",
  ),
  "utf8",
);

test("hides completed execution details and keeps delivered files after the answer", () => {
  const assistantTurnStart = messageListSource.indexOf('data-assistant-turn=');
  const assistantTurnEnd = messageListSource.indexOf(
    "{renderTokenUsage({",
    assistantTurnStart,
  );
  const assistantTurnSource = messageListSource.slice(
    assistantTurnStart,
    assistantTurnEnd,
  );
  const answerIndex = assistantTurnSource.indexOf("{group.messages.map");
  const deliveredFilesIndex = assistantTurnSource.indexOf("<ArtifactFileList");

  expect(messageListSource).not.toContain(
    'from "./assistant-process-disclosure"',
  );
  expect(assistantTurnSource).not.toContain("<AssistantProcessDisclosure");
  expect(assistantTurnSource).not.toContain("showReasoning={!turnDisplay}");
  expect(messageListSource).not.toContain("completedReasoningStatusLabel");
  expect(messageListSource).not.toContain("reasoningStatusLabel=");
  expect(answerIndex).toBeGreaterThan(-1);
  expect(deliveredFilesIndex).toBeGreaterThan(answerIndex);
});
