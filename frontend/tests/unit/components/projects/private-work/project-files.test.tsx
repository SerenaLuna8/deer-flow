import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

describe("project chat files and artifacts", () => {
  test("artifact views resolve project downloads without legacy URL fallback", () => {
    const detail = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/artifacts/artifact-file-detail.tsx",
      ),
      "utf8",
    );
    const list = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/artifacts/artifact-file-list.tsx",
      ),
      "utf8",
    );
    expect(detail).toContain("projectFileDownloadURL");
    expect(detail).toContain("projectArtifactDownloadURL");
    expect(list).toContain("projectFileDownloadURL");
    expect(detail).toContain("useProjectPrivateWorkScope");
    expect(list).toContain("useProjectPrivateWorkScope");
    expect(list).toContain("useDeleteUploadedFile");
    expect(list).toContain("deleteProjectFile.mutateAsync");
  });

  test("project-ready files are exposed in the shared artifact list", () => {
    const chatBox = readFileSync(
      resolve(process.cwd(), "src/components/workspace/chats/chat-box.tsx"),
      "utf8",
    );
    expect(chatBox).toContain("usePrivateWorkAccess");
    expect(chatBox).toContain("useUploadedFiles");
    expect(chatBox).toContain("logical_path");
  });

  test("sidecar restore and create consume the entered project client", () => {
    const panel = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/sidecar/sidecar-panel.tsx",
      ),
      "utf8",
    );
    const context = readFileSync(
      resolve(process.cwd(), "src/components/workspace/sidecar/context.tsx"),
      "utf8",
    );
    expect(panel).toContain("usePrivateWorkAccess");
    expect(panel).toContain("createProjectThread");
    expect(context).toContain("apiClient: privateWork.client");
  });
});
