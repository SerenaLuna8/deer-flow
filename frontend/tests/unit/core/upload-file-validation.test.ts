import { describe, expect, test } from "@rstest/core";

import { validateUploadLimits } from "@/core/uploads/file-validation";
import type { UploadLimits } from "@/core/uploads/limits";

function limits(overrides: Partial<UploadLimits> = {}): UploadLimits {
  return {
    max_files: 5,
    max_file_size: 20,
    max_total_size: 20,
    project_storage: {
      policy: "project_quota",
      remaining_bytes: 3,
    },
    request_id: "request-1",
    ...overrides,
  };
}

describe("validateUploadLimits eager-upload storage accounting", () => {
  test("does not charge a ready file against remaining Project storage twice", () => {
    const ready = new File(["12345678"], "ready.txt");
    const pending = new File(["123"], "pending.txt");

    const result = validateUploadLimits([], [ready, pending], limits(), {
      projectStorageFiles: new Set([pending]),
    });

    expect(result.accepted).toEqual([ready, pending]);
    expect(result.violations).toEqual([]);
  });

  test("still applies message limits to ready files and storage limits to pending files", () => {
    const ready = new File(["12345678"], "ready.txt");
    const pending = new File(["1234"], "pending.txt");

    const storageResult = validateUploadLimits([], [ready, pending], limits(), {
      projectStorageFiles: new Set([pending]),
    });
    expect(storageResult.accepted).toEqual([ready]);
    expect(storageResult.violations).toMatchObject([
      { code: "project_storage_remaining", files: [pending], limit: 3 },
    ]);

    const totalResult = validateUploadLimits(
      [],
      [ready],
      limits({ max_total_size: 7 }),
      { projectStorageFiles: new Set() },
    );
    expect(totalResult.accepted).toEqual([]);
    expect(totalResult.violations).toMatchObject([
      { code: "max_total_size", files: [ready], limit: 7 },
    ]);
  });
});
