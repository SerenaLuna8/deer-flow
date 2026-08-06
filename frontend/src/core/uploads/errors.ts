import type { UploadLimitViolation } from "./file-validation";

export class UploadApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryAfter: string | null;

  constructor(input: {
    status: number;
    code: string;
    message: string;
    requestId?: string | null;
    retryAfter?: string | null;
  }) {
    super(input.message);
    this.name = "UploadApiError";
    this.status = input.status;
    this.code = input.code;
    this.requestId = input.requestId ?? null;
    this.retryAfter = input.retryAfter ?? null;
  }
}

export class UploadLimitValidationError extends Error {
  readonly violations: UploadLimitViolation[];

  constructor(violations: UploadLimitViolation[]) {
    super("Upload limits were exceeded before the request was sent.");
    this.name = "UploadLimitValidationError";
    this.violations = violations;
  }
}

export function uploadFailureMessage(
  error: unknown,
  messages: {
    tooLarge: string;
    storageQuotaExceeded: string;
    preflightRejected: string;
    fallback: string;
  },
): string {
  if (error instanceof UploadLimitValidationError) {
    return messages.preflightRejected;
  }
  if (error instanceof UploadApiError) {
    if (error.status === 413 || error.code === "PRIVATE_WORK_TOO_LARGE") {
      return messages.tooLarge;
    }
    if (
      error.status === 429 ||
      error.code === "PROJECT_STORAGE_QUOTA_EXCEEDED"
    ) {
      return messages.storageQuotaExceeded;
    }
  }
  return messages.fallback;
}
