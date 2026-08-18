import { expect, test } from "@rstest/core";

import {
  isReadyPromptInputFilePart,
  readyPromptInputFileToPart,
  readyPromptInputFileToMessage,
  type PromptInputFilePart,
} from "@/core/uploads";

test("reuses a restored ready attachment as opaque message metadata", () => {
  const readyFile = {
    file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
    filename: "clipboard.png",
    size: 445_553,
    path: "/mnt/user-data/uploads/clipboard.png",
    status: "uploaded" as const,
  };
  const part = readyPromptInputFileToPart(readyFile);

  expect(isReadyPromptInputFilePart(part)).toBe(true);
  expect(readyPromptInputFileToMessage(part)).toEqual(readyFile);
  expect(part.file).toBeUndefined();
  expect(part.url).not.toMatch(/^data:/u);
});

test("rejects a normal browser file as a ready opaque attachment", () => {
  const part = {
    type: "file",
    url: "blob:local",
    filename: "new.png",
    mediaType: "image/png",
  } as PromptInputFilePart;

  expect(isReadyPromptInputFilePart(part)).toBe(false);
  expect(() => readyPromptInputFileToMessage(part)).toThrow(
    "ready uploaded file reference",
  );
});
