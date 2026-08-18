import { describe, expect, it } from "@rstest/core";
import { createElement, type KeyboardEvent } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  findMissingRequiredFields,
  HumanInputCard,
  HumanInputFormFieldInput,
  shouldSubmitHumanInputTextOnKeyDown,
} from "@/components/workspace/messages/human-input-card";
import { I18nContext } from "@/core/i18n/context";
import type {
  HumanInputField,
  HumanInputRequest,
  HumanInputResponse,
} from "@/core/messages/human-input";

const request: HumanInputRequest = {
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

describe("HumanInputCard", () => {
  it("renders request text, options, and the other-answer input", () => {
    const html = renderCard();

    expect(html).toContain('data-human-input-state="open"');
    expect(html).toContain("1 item needs attention");
    expect(html).toContain("Need your help");
    expect(html).toContain("Need the target environment.");
    expect(html).toContain("Which environment should I deploy to?");
    expect(html).toContain("development");
    expect(html).toContain("staging");
    expect(html).toContain('role="radio"');
    expect(html).toContain('aria-checked="false"');
    expect(html).toContain("Other answer");
    expect(html).toContain("Type another answer...");
    expect(html).toContain("Submit answer");
    expect(html).toContain("You can change your selection before submitting.");
    expect(html).toContain('type="text"');
    expect(html).toContain('<form class="relative space-y-4"');
  });

  it("renders answered state as a collapsed read-only disclosure", () => {
    const response: HumanInputResponse = {
      version: 1,
      kind: "human_input_response",
      source: "ask_clarification",
      request_id: "clarification:call-abc",
      response_kind: "option",
      option_id: "option-2",
      value: "staging",
    };
    const html = renderCard({ answeredResponse: response });

    expect(html).toContain('data-human-input-state="answered"');
    expect(html).toContain('aria-live="polite"');
    expect(html).toContain("Answered");
    expect(html).toContain("Answered: staging");
    expect(html).toContain("<details");
    expect(html).not.toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
    expect(html).toContain("<summary");
    expect(html).toContain("Need the target environment.");
    expect(html).toContain("Which environment should I deploy to?");
    expect(html).toContain("development");
    expect(html).toContain('data-human-input-option-selected="true"');
    expect(html).toContain("Your answer");
    expect(html).not.toContain("Type another answer...");
    expect(html).not.toContain("Submit answer");
    expect(html).not.toContain("<form");
  });

  it("renders only the human-readable summary for an answered form", () => {
    const html = renderCard({
      request: {
        ...request,
        version: 2,
        request_id: "clarification:call-form",
        question: "Provide deployment details",
        input_mode: "form",
        options: undefined,
        fields: [
          {
            name: "environment",
            label: "Environment",
            type: "text",
            required: true,
          },
        ],
      },
      answeredResponse: {
        version: 1,
        kind: "human_input_response",
        source: "ask_clarification",
        request_id: "clarification:call-form",
        response_kind: "text",
        value: "Environment: staging",
        form_values: { environment: "staging" },
      },
    });

    expect(html).toContain("Answered: Environment: staging");
    expect(html).not.toContain("[values:");
    expect(html).not.toContain("&quot;environment&quot;");
    expect(html).not.toContain("Submit answer");
  });

  it("does not expose inline values from a legacy answered form", () => {
    const html = renderCard({
      request: {
        ...request,
        version: 2,
        request_id: "clarification:call-form",
        question: "Provide deployment details",
        input_mode: "form",
        options: undefined,
        fields: [
          {
            name: "environment",
            label: "Environment",
            type: "text",
            required: true,
          },
        ],
      },
      answeredResponse: {
        version: 1,
        kind: "human_input_response",
        source: "ask_clarification",
        request_id: "clarification:call-form",
        response_kind: "text",
        value: 'Environment: staging [values: {"environment":"staging"}]',
      },
    });

    expect(html).toContain("Answered: Environment: staging");
    expect(html).not.toContain("[values:");
    expect(html).not.toContain("&quot;environment&quot;");
  });

  it("renders read-only state when no submit handler is available", () => {
    const html = renderCard({ onSubmit: undefined });

    expect(html).toContain("Read only");
    expect(html).toContain("disabled");
  });

  it("renders markdown in question field (bold, lists)", () => {
    const html = renderCard({
      request: {
        ...request,
        question:
          "你想写什么样的小说？\n\n1. **题材/类型**：科幻、奇幻\n2. **篇幅**：短篇、中篇",
        input_mode: "free_text",
        options: undefined,
      },
    });

    expect(html).toContain("题材/类型");
    expect(html).toContain("篇幅");
    expect(html).not.toContain("**题材/类型**");
    expect(html).not.toContain("**篇幅**");
  });

  it("renders all seven form controls with labels and accessible metadata", () => {
    const html = renderCard({
      request: {
        ...request,
        version: 2,
        request_id: "clarification:call-form",
        question: "Please provide the expense details.",
        input_mode: "form",
        options: undefined,
        fields: [
          { name: "title", label: "Title", type: "text", required: true },
          { name: "note", label: "Note", type: "textarea", required: false },
          { name: "amount", label: "Amount", type: "number", required: true },
          {
            name: "category",
            label: "Category",
            type: "select",
            required: true,
            options: [
              { id: "travel", label: "Travel", value: "travel" },
              { id: "meals", label: "Meals", value: "meals" },
            ],
          },
          {
            name: "receipts",
            label: "Receipts",
            type: "multi_select",
            required: false,
            options: [
              { id: "a-1", label: "A-1", value: "A-1" },
              { id: "a-2", label: "A-2", value: "A-2" },
            ],
          },
          {
            name: "urgent",
            label: "Urgent",
            type: "checkbox",
            required: false,
          },
          {
            name: "spent_on",
            label: "Spent on",
            type: "date",
            required: true,
          },
        ],
      },
    });

    for (const text of [
      "Title",
      "Note",
      "Amount",
      "Category",
      "Receipts",
      "Urgent",
      "Spent on",
      "Select...",
      "A-1",
    ]) {
      expect(html).toContain(text);
    }
    expect(html).toContain('type="text"');
    expect(html).toContain("<textarea");
    expect(html).toContain('type="number"');
    expect(html).toContain('role="combobox"');
    expect(html).toContain('role="group"');
    expect(html).toContain('type="checkbox"');
    expect(html).toContain('type="date"');
    expect(html).toContain('aria-required="true"');
    expect(html).toContain('aria-live="polite"');

    const htmlForIds = [...html.matchAll(/<label[^>]*for="([^"]+)"/g)].map(
      (match) => match[1],
    );
    expect(htmlForIds.length).toBe(7);
    for (const id of htmlForIds) {
      expect(html).toContain(`id="${id}"`);
    }
  });

  it("uses valid required and invalid semantics for the multi-select group", () => {
    const requiredGroupHtml = renderCard({
      request: {
        ...request,
        version: 2,
        request_id: "clarification:call-required-group",
        input_mode: "form",
        options: undefined,
        fields: [
          {
            name: "receipts",
            label: "Receipts",
            type: "multi_select",
            required: true,
            options: [
              { id: "a-1", label: "A-1", value: "A-1" },
              { id: "a-2", label: "A-2", value: "A-2" },
            ],
          },
        ],
      },
    });
    const requiredLabel = /<label[^>]*id="([^"]+)"[^>]*>[\s\S]*?<\/label>/.exec(
      requiredGroupHtml,
    );
    const requiredGroup = /<div id="[^"]+"[^>]*role="group"[^>]*>/.exec(
      requiredGroupHtml,
    )?.[0];

    expect(requiredLabel).toBeDefined();
    expect(requiredLabel?.[0]).toContain("Receipts");
    expect(requiredLabel?.[0]).toMatch(/required/i);
    expect(requiredGroup).toContain(`aria-labelledby="${requiredLabel?.[1]}"`);
    expect(requiredGroup).not.toContain("aria-required");

    const invalidGroupHtml = renderToStaticMarkup(
      createElement(HumanInputFormFieldInput, {
        field: {
          name: "receipts",
          label: "Receipts",
          type: "multi_select",
          required: true,
          options: [
            { id: "a-1", label: "A-1", value: "A-1" },
            { id: "a-2", label: "A-2", value: "A-2" },
          ],
        },
        value: [],
        disabled: false,
        selectPlaceholder: "Select...",
        controlId: "receipts-control",
        labelId: "receipts-label",
        invalid: true,
        errorId: "receipts-error",
        onChange: () => undefined,
      }),
    );
    const invalidGroup =
      /<div id="receipts-control"[^>]*role="group"[^>]*>/.exec(
        invalidGroupHtml,
      )?.[0];

    expect(invalidGroup).toBeDefined();
    expect(invalidGroup).toContain('aria-labelledby="receipts-label"');
    expect(invalidGroup).not.toContain("aria-required");
    expect(invalidGroup).toContain('aria-invalid="true"');
    expect(invalidGroup).toContain('aria-describedby="receipts-error"');

    const requiredControls: Array<[string, HumanInputField]> = [
      [
        "required-text",
        {
          name: "details",
          label: "Details",
          type: "text" as const,
          required: true,
        },
      ],
      [
        "required-select",
        {
          name: "category",
          label: "Category",
          type: "select" as const,
          required: true,
          options: [{ id: "travel", label: "Travel", value: "travel" }],
        },
      ],
    ];
    for (const [controlId, field] of requiredControls) {
      const controlHtml = renderToStaticMarkup(
        createElement(HumanInputFormFieldInput, {
          field,
          value: "",
          disabled: false,
          selectPlaceholder: "Select...",
          controlId,
          labelId: `${controlId}-label`,
          invalid: false,
          errorId: `${controlId}-error`,
          onChange: () => undefined,
        }),
      );
      const control = new RegExp(
        `<(?:input|button)[^>]*id="${controlId}"[^>]*>`,
      ).exec(controlHtml)?.[0];

      expect(control).toBeDefined();
      expect(control).toContain('aria-required="true"');
    }
  });

  it("findMissingRequiredFields uses own values and checkbox truth", () => {
    const fields = [
      {
        name: "amount",
        label: "Amount",
        type: "number" as const,
        required: true,
      },
      {
        name: "receipts",
        label: "Receipts",
        type: "multi_select" as const,
        required: true,
        options: [{ id: "a-1", label: "A-1", value: "A-1" }],
      },
      {
        name: "confirmed",
        label: "Confirmed",
        type: "checkbox" as const,
        required: true,
      },
    ];

    expect(
      findMissingRequiredFields(fields, {}).map((field) => field.name),
    ).toEqual(["amount", "receipts", "confirmed"]);
    expect(
      findMissingRequiredFields(fields, {
        amount: "300",
        receipts: ["A-1"],
        confirmed: false,
      }).map((field) => field.name),
    ).toEqual(["confirmed"]);
    expect(
      findMissingRequiredFields(fields, {
        amount: "300",
        receipts: ["A-1"],
        confirmed: true,
      }),
    ).toEqual([]);
  });

  it("does not submit text with Enter while IME composition is active", () => {
    expect(shouldSubmitHumanInputTextOnKeyDown(keyEvent())).toBe(true);
    expect(
      shouldSubmitHumanInputTextOnKeyDown(keyEvent({ shiftKey: true })),
    ).toBe(false);
    expect(
      shouldSubmitHumanInputTextOnKeyDown(keyEvent({ isComposing: true })),
    ).toBe(false);
    expect(
      shouldSubmitHumanInputTextOnKeyDown(keyEvent({ keyCode: 229 })),
    ).toBe(false);
    expect(shouldSubmitHumanInputTextOnKeyDown(keyEvent(), true)).toBe(false);
  });
});

function renderCard(props: Partial<Parameters<typeof HumanInputCard>[0]> = {}) {
  return renderToStaticMarkup(
    createElement(
      I18nContext.Provider,
      {
        value: {
          locale: "en-US",
          setLocale: () => undefined,
        },
      },
      createElement(HumanInputCard, {
        request,
        onSubmit: () => undefined,
        ...props,
      }),
    ),
  );
}

function keyEvent({
  isComposing = false,
  key = "Enter",
  keyCode = 13,
  shiftKey = false,
}: {
  isComposing?: boolean;
  key?: string;
  keyCode?: number;
  shiftKey?: boolean;
} = {}) {
  return {
    key,
    keyCode,
    nativeEvent: { isComposing },
    shiftKey,
  } as unknown as KeyboardEvent<HTMLTextAreaElement>;
}
