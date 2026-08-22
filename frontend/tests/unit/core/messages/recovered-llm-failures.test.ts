import { describe, expect, test } from "@rstest/core";

import {
  parseRecoveredLLMFailures,
  readRecoveredLLMFailures,
  readLatestRecoveredLLMFailures,
  RECOVERED_LLM_FAILURES_KEY,
} from "@/core/messages/recovered-llm-failures";
import { dedupeMessagesByIdentity } from "@/core/threads/message-projection";

const receipt = {
  schema_version: 1,
  failures: [
    {
      attempt: 1,
      max_attempts: 3,
      error_code: "LLM_PROVIDER_UNAVAILABLE",
      reason: "transient",
      disposition: "recovered",
    },
  ],
};

describe("recovered LLM failure receipts", () => {
  test("reads a safe versioned receipt from an assistant message", () => {
    expect(
      readRecoveredLLMFailures({
        type: "ai",
        content: "Recovered answer",
        additional_kwargs: {
          [RECOVERED_LLM_FAILURES_KEY]: receipt,
        },
      }),
    ).toEqual([
      {
        attempt: 1,
        maxAttempts: 3,
        errorCode: "LLM_PROVIDER_UNAVAILABLE",
        reason: "transient",
      },
    ]);
  });

  test.each([
    null,
    { ...receipt, schema_version: 2 },
    { ...receipt, leaked_error: "secret" },
    { ...receipt, failures: [] },
    {
      ...receipt,
      failures: [{ ...receipt.failures[0], error_code: [] }],
    },
    {
      ...receipt,
      failures: [{ ...receipt.failures[0], reason: {} }],
    },
    {
      ...receipt,
      failures: [{ ...receipt.failures[0], error_code: "LLM_REQUEST_FAILED" }],
    },
    {
      ...receipt,
      failures: [{ ...receipt.failures[0], raw_error: "secret" }],
    },
  ])("fails closed for malformed or extended metadata", (value) => {
    expect(parseRecoveredLLMFailures(value)).toEqual([]);
  });

  test("uses the latest cumulative receipt instead of double-counting earlier calls", () => {
    const message = (id: string, failures: unknown[] = receipt.failures) => ({
      id,
      type: "ai" as const,
      content: "Recovered answer",
      additional_kwargs: {
        [RECOVERED_LLM_FAILURES_KEY]: {
          ...receipt,
          failures,
        },
      },
    });

    expect(
      readLatestRecoveredLLMFailures([
        message("model-call-1"),
        message("model-call-2", [
          ...receipt.failures,
          { ...receipt.failures[0], attempt: 2 },
        ]),
      ]),
    ).toHaveLength(2);
  });

  test("keeps the diagnostic payload bounded", () => {
    expect(
      parseRecoveredLLMFailures({
        ...receipt,
        failures: Array.from({ length: 513 }, () => receipt.failures[0]),
      }),
    ).toEqual([]);
  });

  test("history dedupe keeps the later reconciled receipt for one message id", () => {
    const projected = dedupeMessagesByIdentity([
      {
        id: "answer-1",
        type: "ai",
        content: "Recovered answer",
        additional_kwargs: {},
      },
      {
        id: "answer-1",
        type: "ai",
        content: "Recovered answer",
        additional_kwargs: {
          [RECOVERED_LLM_FAILURES_KEY]: receipt,
        },
      },
    ]);

    expect(projected).toHaveLength(1);
    expect(readRecoveredLLMFailures(projected[0]!)).toHaveLength(1);
  });
});
