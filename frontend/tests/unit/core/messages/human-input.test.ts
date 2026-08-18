import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import {
  buildHumanInputFormSummary,
  buildInitialHumanInputFormValues,
  buildHumanInputResponseText,
  createHumanInputFormResponse,
  humanInputResponseDisplayValue,
  createHumanInputOptionResponse,
  createHumanInputTextResponse,
  deriveHumanInputThreadState,
  extractHumanInputRequest,
  extractHumanInputResponse,
  hasOpenHumanInputRequest,
  parseHumanInputRequest,
  readHumanInputFormValue,
  shouldClearPendingHumanInputOnThreadError,
} from "@/core/messages/human-input";

const requestPayload = {
  version: 1,
  kind: "human_input_request",
  source: "ask_clarification",
  request_id: "clarification:call-abc",
  tool_call_id: "call-abc",
  clarification_type: "approach_choice",
  question: "Which environment should I deploy to?",
  context: "Need the target environment.",
  input_mode: "choice_with_other",
  options: [
    { id: "option-1", label: "development", value: "development" },
    { id: "option-2", label: "staging", value: "staging" },
  ],
};

test("extractHumanInputRequest reads a valid tool artifact payload", () => {
  const message = {
    type: "tool",
    name: "ask_clarification",
    content: "fallback",
    artifact: {
      human_input: requestPayload,
    },
  } as unknown as Message;

  expect(extractHumanInputRequest(message)).toEqual(requestPayload);
});

test("extractHumanInputRequest rejects malformed artifacts", () => {
  const message = {
    type: "tool",
    name: "ask_clarification",
    content: "fallback",
    artifact: {
      human_input: {
        ...requestPayload,
        options: [{ id: "option-1", label: "missing value" }],
      },
    },
  } as unknown as Message;

  expect(extractHumanInputRequest(message)).toBeNull();
});

test("extractHumanInputResponse reads valid human message metadata", () => {
  const response = {
    version: 1,
    kind: "human_input_response",
    source: "ask_clarification",
    request_id: "clarification:call-abc",
    response_kind: "option",
    option_id: "option-2",
    value: "staging",
  };
  const message = {
    type: "human",
    content: "For your clarification, my answer is: staging",
    additional_kwargs: {
      hide_from_ui: true,
      human_input_response: response,
    },
  } as unknown as Message;

  expect(extractHumanInputResponse(message)).toEqual(response);
});

test("extractHumanInputResponse preserves structured form values separately", () => {
  const response = {
    version: 1,
    kind: "human_input_response",
    source: "ask_clarification",
    request_id: "clarification:call-form",
    response_kind: "text",
    value: "Environment: staging",
    form_values: { environment: "staging", smoke_test: true },
  };
  const message = {
    type: "human",
    content:
      'For your clarification "Provide deployment details", my answer is: Environment: staging [values: {"environment":"staging","smoke_test":true}]',
    additional_kwargs: {
      hide_from_ui: true,
      human_input_response: response,
    },
  } as unknown as Message;

  expect(extractHumanInputResponse(message)).toEqual(response);
});

test("derives answered card state from hidden human input responses", () => {
  const response = {
    version: 1,
    kind: "human_input_response",
    source: "ask_clarification",
    request_id: "clarification:call-abc",
    response_kind: "option",
    option_id: "option-2",
    value: "staging",
  };
  const state = deriveHumanInputThreadState([
    {
      type: "tool",
      name: "ask_clarification",
      content: "fallback",
      artifact: {
        human_input: requestPayload,
      },
    } as unknown as Message,
    {
      type: "human",
      content: "For your clarification, my answer is: staging",
      additional_kwargs: {
        hide_from_ui: true,
        human_input_response: response,
      },
    } as unknown as Message,
  ]);

  expect(state.answeredResponses.get("clarification:call-abc")).toEqual(
    response,
  );
  expect(state.latestOpenRequestId).toBeNull();
});

test("detects whether a thread has an open human input request", () => {
  const requestMessage = {
    type: "tool",
    name: "ask_clarification",
    content: "fallback",
    artifact: {
      human_input: requestPayload,
    },
  } as unknown as Message;
  const responseMessage = {
    type: "human",
    content: "For your clarification, my answer is: staging",
    additional_kwargs: {
      hide_from_ui: true,
      human_input_response: {
        version: 1,
        kind: "human_input_response",
        source: "ask_clarification",
        request_id: "clarification:call-abc",
        response_kind: "option",
        option_id: "option-2",
        value: "staging",
      },
    },
  } as unknown as Message;

  expect(hasOpenHumanInputRequest([requestMessage])).toBe(true);
  expect(hasOpenHumanInputRequest([requestMessage, responseMessage])).toBe(
    false,
  );
});

test("detects new thread errors that should unlock pending human input cards", () => {
  const previousError = new Error("old failure");
  const currentError = new Error("stream failed");

  expect(
    shouldClearPendingHumanInputOnThreadError({
      currentError,
      pendingRequestCount: 1,
      previousError: undefined,
    }),
  ).toBe(true);
  expect(
    shouldClearPendingHumanInputOnThreadError({
      currentError,
      pendingRequestCount: 0,
      previousError: undefined,
    }),
  ).toBe(false);
  expect(
    shouldClearPendingHumanInputOnThreadError({
      currentError: previousError,
      pendingRequestCount: 1,
      previousError,
    }),
  ).toBe(false);
  expect(
    shouldClearPendingHumanInputOnThreadError({
      currentError: undefined,
      pendingRequestCount: 1,
      previousError: currentError,
    }),
  ).toBe(false);
});

test("creates option and text responses for a request", () => {
  const request = extractHumanInputRequest({
    type: "tool",
    name: "ask_clarification",
    content: "fallback",
    artifact: {
      human_input: requestPayload,
    },
  } as unknown as Message);

  expect(request).not.toBeNull();
  const optionResponse = createHumanInputOptionResponse(
    request!,
    request!.options![1]!,
  );
  const textResponse = createHumanInputTextResponse(
    request!,
    "Use blue-green deployment",
  );

  expect(optionResponse).toEqual({
    version: 1,
    kind: "human_input_response",
    source: "ask_clarification",
    request_id: "clarification:call-abc",
    response_kind: "option",
    option_id: "option-2",
    value: "staging",
  });
  expect(textResponse).toEqual({
    version: 1,
    kind: "human_input_response",
    source: "ask_clarification",
    request_id: "clarification:call-abc",
    response_kind: "text",
    value: "Use blue-green deployment",
  });
  expect(buildHumanInputResponseText(request!, optionResponse)).toBe(
    'For your clarification "Which environment should I deploy to?", my answer is: staging',
  );
});

const formPayload = {
  version: 2,
  kind: "human_input_request",
  source: "ask_clarification",
  request_id: "clarification:call-form",
  question: "Please provide the expense details.",
  input_mode: "form",
  fields: [
    {
      name: "title",
      label: "Title",
      type: "text",
      required: true,
      placeholder: "Expense title",
    },
    { name: "note", label: "Note", type: "textarea", required: false },
    { name: "amount", label: "Amount", type: "number", required: true },
    {
      name: "category",
      label: "Category",
      type: "select",
      required: true,
      options: [
        { id: "category-option-1", label: "travel", value: "travel" },
        { id: "category-option-2", label: "meals", value: "meals" },
      ],
    },
    {
      name: "receipts",
      label: "Receipts",
      type: "multi_select",
      required: false,
      options: [
        { id: "receipts-option-1", label: "A-1", value: "A-1" },
        { id: "receipts-option-2", label: "A-2", value: "A-2" },
      ],
    },
    { name: "urgent", label: "Urgent", type: "checkbox", required: false },
    { name: "spent_on", label: "Spent on", type: "date", required: true },
  ],
};

function toolMessage(payload: unknown): Message {
  return {
    type: "tool",
    name: "ask_clarification",
    content: "fallback",
    artifact: { human_input: payload },
  } as unknown as Message;
}

test("parses all seven v2 form field types", () => {
  expect(extractHumanInputRequest(toolMessage(formPayload))).toEqual(
    formPayload,
  );
});

test("keeps v1 free text valid but rejects v1 payloads carrying form fields", () => {
  const freeTextPayload = {
    ...requestPayload,
    input_mode: "free_text",
    options: undefined,
  };

  expect(extractHumanInputRequest(toolMessage(freeTextPayload))).toEqual(
    freeTextPayload,
  );
  expect(
    extractHumanInputRequest(
      toolMessage({
        ...freeTextPayload,
        fields: [
          {
            name: "details",
            label: "Details",
            type: "textarea",
            required: false,
          },
        ],
      }),
    ),
  ).toBeNull();
  expect(extractHumanInputRequest(toolMessage(formPayload))).toEqual(
    formPayload,
  );
});

test("rejects malformed form fields, versions, and mode bindings", () => {
  expect(
    extractHumanInputRequest(toolMessage({ ...formPayload, fields: [] })),
  ).toBeNull();
  expect(
    extractHumanInputRequest(
      toolMessage({
        ...formPayload,
        fields: [{ label: "missing name", type: "text" }],
      }),
    ),
  ).toBeNull();
  expect(
    extractHumanInputRequest(
      toolMessage({
        ...formPayload,
        fields: [
          {
            name: "amount",
            label: "Amount",
            type: "slider",
            required: true,
          },
        ],
      }),
    ),
  ).toBeNull();
  expect(
    extractHumanInputRequest(toolMessage({ ...formPayload, version: 3 })),
  ).toBeNull();
  expect(
    extractHumanInputRequest(toolMessage({ ...formPayload, version: 1 })),
  ).toBeNull();
  expect(
    extractHumanInputRequest(toolMessage({ ...requestPayload, version: 2 })),
  ).toBeNull();
});

test("rejects empty or duplicate option values and duplicate field names", () => {
  const withOptions = (
    options: Array<{ id: string; label: string; value: string }>,
  ) =>
    extractHumanInputRequest(
      toolMessage({
        ...formPayload,
        fields: [
          {
            name: "category",
            label: "Category",
            type: "select",
            required: true,
            options,
          },
        ],
      }),
    );

  expect(withOptions([{ id: "o1", label: "travel", value: "" }])).toBeNull();
  expect(
    withOptions([
      { id: "o1", label: "travel", value: "travel" },
      { id: "o1", label: "meals", value: "meals" },
    ]),
  ).toBeNull();
  expect(
    withOptions([
      { id: "o1", label: "travel", value: "travel" },
      { id: "o2", label: "travel again", value: "travel" },
    ]),
  ).toBeNull();
  expect(
    extractHumanInputRequest(
      toolMessage({
        ...formPayload,
        fields: [
          { name: "amount", label: "Amount", type: "number", required: true },
          { name: "amount", label: "Again", type: "text", required: false },
        ],
      }),
    ),
  ).toBeNull();
});

test("rejects form fields with reserved prototype names", () => {
  for (const reserved of [
    "__proto__",
    "constructor",
    "prototype",
    "toString",
    "hasOwnProperty",
  ]) {
    expect(
      extractHumanInputRequest(
        toolMessage({
          ...formPayload,
          fields: [
            { name: reserved, label: "Unsafe", type: "text", required: true },
          ],
        }),
      ),
    ).toBeNull();
  }
});

test("form values use own-property reads and seed checkbox false", () => {
  const values: Record<string, string> = { amount: "300" };
  expect(readHumanInputFormValue(values, "amount")).toBe("300");
  expect(readHumanInputFormValue(values, "toString")).toBeUndefined();
  expect(readHumanInputFormValue(values, "constructor")).toBeUndefined();
  expect(readHumanInputFormValue(values, "__proto__")).toBeUndefined();

  const request = extractHumanInputRequest(toolMessage(formPayload))!;
  expect(buildInitialHumanInputFormValues(request.fields ?? [])).toEqual({
    urgent: false,
  });
});

test("separates the visible form summary from structured submission values", () => {
  const request = extractHumanInputRequest(toolMessage(formPayload))!;
  const values = {
    receipts: ["A-1", "A-2"],
    urgent: false,
    category: "travel",
    amount: "300",
    title: "Taxi",
    spent_on: "2026-07-30",
    note: "",
  };

  expect(buildHumanInputFormSummary(request, values)).toBe(
    "Title: Taxi; Amount: 300; Category: travel; Receipts: A-1, A-2; Urgent: no; Spent on: 2026-07-30",
  );
  const response = createHumanInputFormResponse(request, values);
  expect(response.value).toBe(
    "Title: Taxi; Amount: 300; Category: travel; Receipts: A-1, A-2; Urgent: no; Spent on: 2026-07-30",
  );
  expect(response.form_values).toEqual({
    title: "Taxi",
    amount: "300",
    category: "travel",
    receipts: ["A-1", "A-2"],
    urgent: false,
    spent_on: "2026-07-30",
  });
  expect(buildHumanInputResponseText(request, response)).toBe(
    'For your clarification "Please provide the expense details.", my answer is: Title: Taxi; Amount: 300; Category: travel; Receipts: A-1, A-2; Urgent: no; Spent on: 2026-07-30 [values: {"title":"Taxi","amount":"300","category":"travel","receipts":["A-1","A-2"],"urgent":false,"spent_on":"2026-07-30"}]',
  );
});

test("keeps an optional-only empty form response valid", () => {
  const request = parseHumanInputRequest({
    version: 2,
    kind: "human_input_request",
    source: "ask_clarification",
    request_id: "clarification:optional-form",
    question: "Add optional details",
    input_mode: "form",
    fields: [
      {
        name: "note",
        label: "Note",
        type: "textarea",
        required: false,
      },
    ],
  });
  expect(request).not.toBeNull();

  const response = createHumanInputFormResponse(request!, {});
  expect(response.value).toBe("-");
  expect(response.form_values).toEqual({});
  expect(
    extractHumanInputResponse({
      type: "human",
      content: "",
      additional_kwargs: { human_input_response: response },
    } as unknown as Message),
  ).toEqual(response);
  expect(
    humanInputResponseDisplayValue(request!, {
      ...response,
      form_values: undefined,
      value: " [values: {}]",
    }),
  ).toBe("-");
});

test("hides legacy inline form values from the answered-card display", () => {
  const request = parseHumanInputRequest({
    version: 2,
    kind: "human_input_request",
    source: "ask_clarification",
    request_id: "clarification:legacy-form",
    question: "Provide deployment details",
    input_mode: "form",
    fields: [
      {
        name: "environment",
        label: "Environment",
        type: "text",
        required: true,
      },
    ],
  });
  expect(request).not.toBeNull();

  expect(
    humanInputResponseDisplayValue(request!, {
      version: 1,
      kind: "human_input_response",
      source: "ask_clarification",
      request_id: "clarification:legacy-form",
      response_kind: "text",
      value: 'Environment: staging [values: {"environment":"staging"}]',
    }),
  ).toBe("Environment: staging");
});

test("a visible plain human reply closes only the latest unanswered request", () => {
  const olderPayload = {
    ...formPayload,
    request_id: "clarification:call-older",
  };
  const state = deriveHumanInputThreadState([
    toolMessage(olderPayload),
    toolMessage(formPayload),
    {
      type: "human",
      content: [
        { type: "text", text: "answer to " },
        { type: "text", text: "the latest question" },
      ],
    } as unknown as Message,
  ]);

  expect(state.answeredResponses.get("clarification:call-form")?.value).toBe(
    "answer to the latest question",
  );
  expect(state.answeredResponses.has("clarification:call-older")).toBe(false);
  expect(state.latestOpenRequestId).toBe("clarification:call-older");
});

test("hidden plain human messages do not close an open request", () => {
  const state = deriveHumanInputThreadState([
    toolMessage(formPayload),
    {
      type: "human",
      content: "internal context",
      additional_kwargs: { hide_from_ui: true },
    } as unknown as Message,
  ]);

  expect(state.latestOpenRequestId).toBe("clarification:call-form");
});
