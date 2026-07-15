import { describe, expect, test } from "@rstest/core";

import {
  projectArtifactDownloadURL,
  projectFileDownloadURL,
  resolveProjectArtifactReferenceURL,
} from "@/core/private-work/files";

const access = {
  apiBaseURL: "/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work",
};

describe("project file URL adapter", () => {
  test("builds encoded project file and artifact download URLs", () => {
    expect(projectFileDownloadURL(access, "thread/1", "file/1")).toBe(
      `${access.apiBaseURL}/threads/thread%2F1/files/file%2F1`,
    );
    expect(projectArtifactDownloadURL(access, "thread/1", "artifact/1")).toBe(
      `${access.apiBaseURL}/artifacts/artifact%2F1?thread_id=thread%2F1`,
    );
  });

  test("never accepts a host filesystem path as a project download base", () => {
    expect(() =>
      projectFileDownloadURL(
        { apiBaseURL: "/Users/alice/project" },
        "thread-1",
        "file-1",
      ),
    ).toThrow();
  });

  test("resolves logical paths through scoped file ids and UUIDs through artifacts", () => {
    expect(
      resolveProjectArtifactReferenceURL(
        access,
        "thread/1",
        "workspace/report.md",
        [
          {
            id: "55555555-5555-4555-8555-555555555555",
            logicalPath: "workspace/report.md",
          },
        ],
      ),
    ).toBe(
      `${access.apiBaseURL}/threads/thread%2F1/files/55555555-5555-4555-8555-555555555555`,
    );
    expect(
      resolveProjectArtifactReferenceURL(
        access,
        "thread/1",
        "66666666-6666-4666-8666-666666666666",
        [],
      ),
    ).toBe(
      `${access.apiBaseURL}/artifacts/66666666-6666-4666-8666-666666666666?thread_id=thread%2F1`,
    );
    expect(
      resolveProjectArtifactReferenceURL(
        access,
        "thread/1",
        "/Users/alice/report.md",
        [],
      ),
    ).toBeNull();
  });
});
