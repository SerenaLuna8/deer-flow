import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import {
  buildThreadSubmitMessages,
  uploadedFileInfoToMessage,
} from "@/core/threads/hooks";

test("builds thread submit messages with hidden sidecar context before the visible user message", () => {
  const hiddenContext = {
    type: "human",
    content: "Hidden sidecar context",
    additional_kwargs: {
      hide_from_ui: true,
      sidecar_context: true,
    },
  } as Message;

  const messages = buildThreadSubmitMessages({
    text: "What should we do next?",
    messageId: "human-visible-1",
    additionalInputMessages: [hiddenContext],
  });

  expect(messages).toEqual([
    hiddenContext,
    {
      type: "human",
      id: "human-visible-1",
      content: [{ type: "text", text: "What should we do next?" }],
      additional_kwargs: {},
    },
  ]);
});

test("keeps uploaded files on the visible user message only", () => {
  const messages = buildThreadSubmitMessages({
    text: "Use this file",
    messageId: "human-visible-2",
    additionalInputMessages: [
      {
        type: "human",
        content: "Hidden sidecar context",
        additional_kwargs: { hide_from_ui: true },
      } as Message,
    ],
    filesForSubmit: [
      {
        file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
        filename: "report.pdf",
        size: 42,
        path: "/uploads/report.pdf",
        status: "uploaded",
      },
    ],
  });

  expect(messages[0]?.additional_kwargs).toEqual({ hide_from_ui: true });
  expect(messages[1]?.additional_kwargs).toEqual({
    files: [
      {
        file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
        filename: "report.pdf",
        size: 42,
        path: "/uploads/report.pdf",
        status: "uploaded",
      },
    ],
  });
});

test("maps a private upload response to current-run file authority metadata", () => {
  expect(
    uploadedFileInfoToMessage({
      id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
      kind: "upload",
      filename: "report.pdf",
      size: 42,
      path: "/mnt/user-data/uploads/report.pdf",
      virtual_path: "/mnt/user-data/uploads/report.pdf",
      artifact_url: "/api/files/opaque",
    }),
  ).toEqual({
    file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
    filename: "report.pdf",
    size: 42,
    path: "/mnt/user-data/uploads/report.pdf",
    status: "uploaded",
  });
});

test("rejects an uploaded response without an opaque file id", () => {
  expect(() =>
    uploadedFileInfoToMessage({
      filename: "report.pdf",
      size: 42,
      path: "/mnt/user-data/uploads/report.pdf",
      virtual_path: "/mnt/user-data/uploads/report.pdf",
      artifact_url: "/api/files/opaque",
    }),
  ).toThrow("Uploaded file response is missing its opaque id");
});

test("rejects an uploaded response with a malformed opaque file id", () => {
  expect(() =>
    uploadedFileInfoToMessage({
      id: "not-an-opaque-uuid",
      filename: "report.pdf",
      size: 42,
      path: "/mnt/user-data/uploads/report.pdf",
      virtual_path: "/mnt/user-data/uploads/report.pdf",
      artifact_url: "/api/files/opaque",
    }),
  ).toThrow("Uploaded file response has an invalid opaque id");
});

test("keeps human input response metadata on the hidden user message", () => {
  const response = {
    version: 1,
    kind: "human_input_response",
    source: "ask_clarification",
    request_id: "clarification:call-abc",
    response_kind: "option",
    option_id: "option-2",
    value: "staging",
  };

  const messages = buildThreadSubmitMessages({
    text: 'For your clarification "Which environment?", my answer is: staging',
    messageId: "human-hidden-response",
    additionalKwargs: {
      hide_from_ui: true,
      human_input_response: response,
    },
  });

  expect(messages).toEqual([
    {
      type: "human",
      id: "human-hidden-response",
      content: [
        {
          type: "text",
          text: 'For your clarification "Which environment?", my answer is: staging',
        },
      ],
      additional_kwargs: {
        hide_from_ui: true,
        human_input_response: response,
      },
    },
  ]);
});
