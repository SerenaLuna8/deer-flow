import { describe, expect, test } from "@rstest/core";

import type { SkillAssetVersion } from "@/components/projects/assets/skill-asset-detail";
import {
  addSkillFile,
  deleteSkillFile,
  editSkillFile,
  listWorkingSkillFiles,
  markdownPreviewContent,
  renameSkillFile,
} from "@/components/projects/assets/skill-file-workbench-state";

const files: SkillAssetVersion["file_views"] = [
  {
    path: "scripts/run.py",
    media_type: "text/x-python",
    size_bytes: 12,
    sha256: "a".repeat(64),
  },
  {
    path: "SKILL.md",
    media_type: "text/markdown",
    size_bytes: 20,
    sha256: "b".repeat(64),
  },
];

describe("Skill file workbench state", () => {
  test("keeps SKILL.md first and marks edits without mutating the source list", () => {
    const changes = editSkillFile([], files, "SKILL.md", "# Updated\n");
    const working = listWorkingSkillFiles(files, changes);

    expect(working.map((file) => [file.path, file.state])).toEqual([
      ["SKILL.md", "modified"],
      ["scripts/run.py", "unchanged"],
    ]);
    expect(files[0]?.path).toBe("scripts/run.py");
    expect(changes).toEqual([
      {
        op: "replace",
        path: "SKILL.md",
        content: "# Updated\n",
        media_type: "text/markdown",
      },
    ]);
  });

  test("adds, renames, and deletes text files as unique fork changes", () => {
    let changes = addSkillFile(
      [],
      files,
      "references/guide.md",
      "Guide",
      "text/markdown",
    );
    changes = renameSkillFile(
      changes,
      files,
      "references/guide.md",
      "references/start-here.md",
      "Guide",
      "text/markdown",
    );
    changes = deleteSkillFile(changes, files, "scripts/run.py");

    expect(changes).toEqual([
      {
        op: "create",
        path: "references/start-here.md",
        content: "Guide",
        media_type: "text/markdown",
      },
      { op: "delete", path: "scripts/run.py" },
    ]);
    expect(
      listWorkingSkillFiles(files, changes).map((file) => file.path),
    ).toEqual(["SKILL.md", "references/start-here.md"]);
  });

  test("protects SKILL.md and rejects duplicate or unsafe paths", () => {
    expect(() => deleteSkillFile([], files, "SKILL.md")).toThrow();
    expect(() =>
      renameSkillFile(
        [],
        files,
        "SKILL.md",
        "README.md",
        "manifest",
        "text/markdown",
      ),
    ).toThrow();
    expect(() =>
      addSkillFile([], files, "scripts/run.py", "duplicate", "text/plain"),
    ).toThrow();
    expect(() =>
      addSkillFile([], files, "../escape.md", "bad", "text/plain"),
    ).toThrow();
  });

  test("hides YAML frontmatter only in the rendered Markdown preview", () => {
    const source =
      "---\nname: paper-review\ndescription: Review papers\n---\n\n# Paper Review\n\nUse the checklist.";

    expect(markdownPreviewContent(source)).toBe(
      "# Paper Review\n\nUse the checklist.",
    );
    expect(markdownPreviewContent("# Plain Markdown")).toBe("# Plain Markdown");
    expect(source).toContain("name: paper-review");
  });
});
