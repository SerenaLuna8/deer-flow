import { describe, expect, test } from "@rstest/core";

import type { SkillAssetVersion } from "@/components/projects/assets/skill-asset-detail";
import {
  addSkillFile,
  addSkillFolder,
  buildSkillFileTree,
  deleteSkillFile,
  defaultSkillFileFolder,
  editSkillFile,
  joinSkillFilePath,
  listSkillFolderPaths,
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

  test("turns rename A to B to A into a replace instead of conflicting create", () => {
    let changes = renameSkillFile(
      [],
      files,
      "scripts/run.py",
      "scripts/renamed.py",
      "print('run')\n",
      "text/x-python",
    );
    changes = renameSkillFile(
      changes,
      files,
      "scripts/renamed.py",
      "scripts/run.py",
      "print('run')\n",
      "text/x-python",
    );

    expect(changes).toEqual([
      {
        op: "replace",
        path: "scripts/run.py",
        content: "print('run')\n",
        media_type: "text/x-python",
      },
    ]);
  });

  test("turns delete then add at an original path into a replace", () => {
    let changes = deleteSkillFile([], files, "scripts/run.py");
    changes = addSkillFile(
      changes,
      files,
      "scripts/run.py",
      "print('replacement')\n",
      "text/x-python",
    );

    expect(changes).toEqual([
      {
        op: "replace",
        path: "scripts/run.py",
        content: "print('replacement')\n",
        media_type: "text/x-python",
      },
    ]);
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
      addSkillFile([], files, "scripts", "file-folder collision", "text/plain"),
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

  test("builds a real nested tree and keeps explicitly-created empty folders", () => {
    const working = listWorkingSkillFiles(files, []);
    const folders = addSkillFolder(
      ["references"],
      working,
      "references",
      "examples",
    );

    expect(listSkillFolderPaths(working, folders)).toEqual([
      "references",
      "references/examples",
      "scripts",
    ]);
    expect(buildSkillFileTree(working, folders)).toEqual([
      {
        kind: "file",
        name: "SKILL.md",
        path: "SKILL.md",
        file: working[0],
      },
      {
        kind: "folder",
        name: "references",
        path: "references",
        children: [
          {
            kind: "folder",
            name: "examples",
            path: "references/examples",
            children: [],
          },
        ],
      },
      {
        kind: "folder",
        name: "scripts",
        path: "scripts",
        children: [
          {
            kind: "file",
            name: "run.py",
            path: "scripts/run.py",
            file: working[1],
          },
        ],
      },
    ]);
  });

  test("defaults new files to a selected folder or the selected file parent", () => {
    expect(defaultSkillFileFolder({ kind: "folder", path: "references" })).toBe(
      "references",
    );
    expect(
      defaultSkillFileFolder({
        kind: "file",
        path: "references/guide.md",
      }),
    ).toBe("references");
    expect(defaultSkillFileFolder({ kind: "file", path: "SKILL.md" })).toBe("");
    expect(defaultSkillFileFolder(null)).toBe("");
  });

  test("creates files from separate folder and filename inputs", () => {
    expect(joinSkillFilePath("references", "guide.md")).toBe(
      "references/guide.md",
    );
    expect(joinSkillFilePath("", "notes.md")).toBe("notes.md");
    expect(() => joinSkillFilePath("references", "nested/guide.md")).toThrow();
    expect(() =>
      addSkillFolder(["references"], files, "", "references"),
    ).toThrow();
    expect(() => addSkillFolder([], files, "", "SKILL.md")).toThrow();
  });
});
