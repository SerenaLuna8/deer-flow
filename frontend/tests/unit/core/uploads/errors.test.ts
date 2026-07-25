import { expect, test } from "@rstest/core";

import {
  UploadApiError,
  UploadLimitValidationError,
  uploadFailureMessage,
} from "@/core/uploads/errors";

const messages = {
  tooLarge: "too large",
  storageQuotaExceeded: "storage exhausted",
  preflightRejected: "preflight rejected",
  fallback: "upload failed",
};

test("maps authoritative 413 and 429 upload failures to distinct UI messages", () => {
  expect(
    uploadFailureMessage(
      new UploadApiError({
        status: 413,
        code: "PRIVATE_WORK_TOO_LARGE",
        message: "public",
      }),
      messages,
    ),
  ).toBe("too large");
  expect(
    uploadFailureMessage(
      new UploadApiError({
        status: 429,
        code: "PROJECT_STORAGE_QUOTA_EXCEEDED",
        message: "public",
      }),
      messages,
    ),
  ).toBe("storage exhausted");
  expect(
    uploadFailureMessage(
      new UploadLimitValidationError([
        { code: "max_total_size", files: [], limit: 1 },
      ]),
      messages,
    ),
  ).toBe("preflight rejected");
});
