import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@rstest/core";

const messageListSource = readFileSync(
  resolve(process.cwd(), "src/components/workspace/messages/message-list.tsx"),
  "utf8",
);

test("keeps all completed execution reasoning before the final answer", () => {
  const assistantTurnStart = messageListSource.indexOf("data-assistant-turn=");
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

  expect(messageListSource).toContain('from "./assistant-process-disclosure"');
  const processIndex = assistantTurnSource.indexOf(
    "<AssistantProcessDisclosure",
  );
  expect(messageListSource).toContain('group.type === "assistant"');
  expect(messageListSource).toContain('"completed-final-reasoning-"');
  expect(assistantTurnSource).toContain(
    "!turnDisplay?.processGroupIndexes.includes(",
  );
  expect(messageListSource).not.toContain("completedReasoningStatusLabel");
  expect(messageListSource).not.toContain("reasoningStatusLabel=");
  expect(processIndex).toBeGreaterThan(-1);
  expect(answerIndex).toBeGreaterThan(-1);
  expect(answerIndex).toBeGreaterThan(processIndex);
  expect(deliveredFilesIndex).toBeGreaterThan(answerIndex);
});
