import type { Message } from "@langchain/langgraph-sdk";
import { z } from "zod";

import type { FileInMessage } from "../messages/utils";
import type { UploadedFileInfo } from "../uploads";

export type SendMessageOptions = {
  additionalKwargs?: Record<string, unknown>;
  additionalInputMessages?: Message[];
  continueFromLatestCheckpoint?: boolean;
  /**
   * Invoked exactly once after the upload and thread submission succeed. It
   * never fires for a dropped concurrent send or a failed submission, so
   * callers can safely clear recoverable one-time composer state.
   */
  onSent?: () => void;
};

export function buildThreadSubmitCheckpointOptions(
  continueFromLatestCheckpoint: boolean | undefined,
): { checkpoint: null } | Record<string, never> {
  return continueFromLatestCheckpoint ? { checkpoint: null } : {};
}

export function buildThreadSubmitMessages({
  text,
  messageId,
  additionalKwargs,
  additionalInputMessages = [],
  filesForSubmit = [],
}: {
  text: string;
  messageId: string;
  additionalKwargs?: Record<string, unknown>;
  additionalInputMessages?: Message[];
  filesForSubmit?: FileInMessage[];
}): Message[] {
  return [
    ...additionalInputMessages,
    {
      type: "human",
      id: messageId,
      content: [
        {
          type: "text",
          text,
        },
      ],
      additional_kwargs: {
        ...additionalKwargs,
        ...(filesForSubmit.length > 0 ? { files: filesForSubmit } : {}),
      },
    } as Message,
  ];
}

export function uploadedFileInfoToMessage(
  info: UploadedFileInfo,
): FileInMessage {
  if (!info.id) {
    throw new TypeError("Uploaded file response is missing its opaque id");
  }
  if (!z.string().uuid().safeParse(info.id).success) {
    throw new TypeError("Uploaded file response has an invalid opaque id");
  }
  return {
    file_id: info.id,
    filename: info.filename,
    size: info.size,
    path: info.virtual_path,
    status: "uploaded",
  };
}
