import type { Message } from "@langchain/langgraph-sdk";

export const RECOVERED_LLM_FAILURES_KEY = "deerflow_recovered_llm_failures";
const MAX_RECOVERED_LLM_FAILURES = 512;

const ERROR_CODE_BY_REASON = {
  quota: "LLM_QUOTA_EXCEEDED",
  auth: "LLM_AUTHENTICATION_FAILED",
  busy: "LLM_PROVIDER_BUSY",
  transient: "LLM_PROVIDER_UNAVAILABLE",
  generic: "LLM_REQUEST_FAILED",
  current_upload: "CURRENT_UPLOAD_UNAVAILABLE",
  circuit_open: "LLM_CIRCUIT_OPEN",
} as const;

export type RecoveredLLMFailureReason = keyof typeof ERROR_CODE_BY_REASON;

export type RecoveredLLMFailure = {
  attempt: number;
  maxAttempts: number;
  errorCode: (typeof ERROR_CODE_BY_REASON)[RecoveredLLMFailureReason];
  reason: RecoveredLLMFailureReason;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => key in value);
}

export function parseRecoveredLLMFailures(
  value: unknown,
): RecoveredLLMFailure[] {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["schema_version", "failures"]) ||
    value.schema_version !== 1 ||
    !Array.isArray(value.failures) ||
    value.failures.length === 0 ||
    value.failures.length > MAX_RECOVERED_LLM_FAILURES
  ) {
    return [];
  }

  const failures: RecoveredLLMFailure[] = [];
  for (const raw of value.failures) {
    if (
      !isRecord(raw) ||
      !hasExactKeys(raw, [
        "attempt",
        "max_attempts",
        "error_code",
        "reason",
        "disposition",
      ]) ||
      !Number.isInteger(raw.attempt) ||
      !Number.isInteger(raw.max_attempts) ||
      typeof raw.attempt !== "number" ||
      typeof raw.max_attempts !== "number" ||
      raw.attempt < 1 ||
      raw.attempt >= raw.max_attempts ||
      typeof raw.reason !== "string" ||
      !(raw.reason in ERROR_CODE_BY_REASON) ||
      raw.error_code !==
        ERROR_CODE_BY_REASON[raw.reason as RecoveredLLMFailureReason] ||
      raw.disposition !== "recovered"
    ) {
      return [];
    }
    const reason = raw.reason as RecoveredLLMFailureReason;
    failures.push({
      attempt: raw.attempt,
      maxAttempts: raw.max_attempts,
      errorCode: ERROR_CODE_BY_REASON[reason],
      reason,
    });
  }
  return failures;
}

export function readRecoveredLLMFailures(
  message: Message,
): RecoveredLLMFailure[] {
  if (message.type !== "ai") {
    return [];
  }
  return parseRecoveredLLMFailures(
    message.additional_kwargs?.[RECOVERED_LLM_FAILURES_KEY],
  );
}

export function readLatestRecoveredLLMFailures(
  messages: readonly Message[],
): RecoveredLLMFailure[] {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message) {
      continue;
    }
    const failures = readRecoveredLLMFailures(message);
    if (failures.length > 0) {
      return failures;
    }
  }
  return [];
}
